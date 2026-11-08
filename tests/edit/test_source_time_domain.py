"""VP-B04：Source 时间域——帧/PTS/时间码换算、In/Out 校验与按资产状态。

缺口（计划 VP-B04）：Source 用独立 float fps 近似，无源起始时间码概念，
VFR 索引（Scene Cut 的 ``SourceFrameTimeIndex``）不参与普通播放；In/Out
提交前的区间校验（空范围、In>Out、负值、超末帧、单帧、只音频）没有统
一权威；换源后的监看状态（播放头/In/Out）恢复没有媒体版本校验。

契约（``SourceTimeDomain`` + ``SourceMonitorStateStore``，纯领域）：

- 有理数帧率：``rational_from_float_fps`` 把 23.976/29.97/59.94 映射到
  24000/1001 等 Rational（不用 ``round(fps)`` 近似）；
- 时间码：帧 → ``Timecode``（NDF/DF 由帧率自动判定），支持非零源起始
  TC 偏移；解析非法 DF 时间码（整点分钟首帧）被显式拒绝；
- VFR：带 ``time_index`` 时帧↔秒走 PTS 索引（二分/插值），CFR 回退
  ``frame/fps``；
- In/Out：Out 为**包含**语义，编辑长度 = out − in + 1；In=Out 合法
  （一帧范围，长度 1）；空/In>Out/负值/超末帧/无帧各回稳定原因；
- 状态存储：按 asset_id + 媒体指纹保存播放头与 In/Out；恢复前验证媒
  体版本（指纹/帧数变化 → 作废返回 None）；只存源域状态，绝不触碰
  时间线范围。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from src.features.edit.core.scene_cut.models import (
    FrameTimePoint,
    SourceFrameTimeIndex,
)
from src.features.edit.core.source_time_domain import (
    REASON_IN_AFTER_OUT,
    REASON_NEGATIVE,
    REASON_NO_FRAMES,
    REASON_PAST_END,
    REASON_RANGE_EMPTY,
    SourceMonitorStateStore,
    SourceTimeDomain,
    rational_from_float_fps,
)
from src.shared.media.metadata import FPS_23_976, FPS_24, FPS_25, FPS_29_97, Rational
from src.shared.media.timecode import Timecode

pytestmark = pytest.mark.edit_core


def _domain(fps=Rational(24, 1), total=2400, start_tc=None, index=None,
            asset="a1") -> SourceTimeDomain:
    return SourceTimeDomain(
        asset_id=asset, media_fingerprint="f1", fps=fps,
        total_frames=total, start_timecode=start_tc, time_index=index)


# ── 帧率：float → Rational（不再 round 近似） ──────────────────────


def test_b04_rational_from_float_fps_common_rates():
    assert rational_from_float_fps(24.0) == FPS_24
    assert rational_from_float_fps(23.976) == FPS_23_976
    assert rational_from_float_fps(29.97) == FPS_29_97
    assert rational_from_float_fps(25.0) == FPS_25
    assert rational_from_float_fps(59.94) == Rational(60000, 1001)
    # 不常见的帧率：有界分母分数兜底，不抛错
    odd = rational_from_float_fps(12.5)
    assert float(odd) == pytest.approx(12.5)


# ── CFR 时间码：NDF/DF、往返、起始 TC ─────────────────────────────


def test_b04_ndf_24fps_roundtrip():
    domain = _domain(FPS_24)
    assert domain.frame_to_timecode(0).to_string() == "00:00:00:00"
    assert domain.frame_to_timecode(1000).to_string() == "00:00:41:16"
    for frame in (0, 1, 23, 24, 999, 239):
        assert domain.timecode_to_frame(
            domain.frame_to_timecode(frame).to_string()) == frame


def test_b04_23_976_is_ndf():
    domain = _domain(FPS_23_976)
    tc = domain.frame_to_timecode(1000)
    assert ";" not in tc.to_string()          # 23.976 不是 DF 帧率
    assert domain.timecode_to_frame(tc.to_string()) == 1000


def test_b04_df_2997_roundtrip_and_spot():
    domain = _domain(FPS_29_97, total=20000)
    # 第 1 分钟丢 2 帧：1800 帧显示 00:01:00;02
    assert domain.frame_to_timecode(1800).to_string() == "00:01:00;02"
    for frame in (0, 1799, 1800, 1801, 3597, 10789):
        assert domain.timecode_to_frame(
            domain.frame_to_timecode(frame).to_string()) == frame


def test_b04_df_illegal_timecode_rejected():
    domain = _domain(FPS_29_97)
    # DF 丢帧分钟（非整十分钟）的首两帧不存在
    with pytest.raises(ValueError):
        domain.timecode_to_frame("00:01:00;00")
    with pytest.raises(ValueError):
        domain.timecode_to_frame("00:01:00;01")
    # 整十分钟不丢帧：合法
    assert domain.timecode_to_frame("00:00:00;00") == 0


def test_b04_nonzero_start_timecode_offsets():
    start = Timecode.from_string("01:00:00:00", FPS_25)
    domain = _domain(FPS_25, total=1000, start_tc=start)
    assert domain.frame_to_timecode(0).to_string() == "01:00:00:00"
    assert domain.frame_to_timecode(100).to_string() == "01:00:04:00"
    assert domain.timecode_to_frame("01:00:04:00") == 100
    # 起始 TC 之前的时间码越界拒绝
    with pytest.raises(ValueError):
        domain.timecode_to_frame("00:59:59:00")


def test_b04_timecode_past_end_rejected():
    domain = _domain(FPS_24, total=240)
    with pytest.raises(ValueError):
        domain.timecode_to_frame("00:00:10:00")  # frame 240 == 总帧数（末帧 239）


# ── VFR：PTS 索引换算 ──────────────────────────────────────────────


def _vfr_index() -> SourceFrameTimeIndex:
    seconds = (0.0, 0.04, 0.10, 0.14, 0.25)
    points = tuple(
        FrameTimePoint(source_frame=i, pts=i * 1000, seconds=s)
        for i, s in enumerate(seconds))
    return SourceFrameTimeIndex(
        asset_id="a1", media_fingerprint="f1", points=points,
        average_fps=len(points) / seconds[-1])


def test_b04_vfr_frame_to_seconds_uses_index():
    domain = _domain(Rational(24, 1), total=5, index=_vfr_index())
    assert domain.frame_to_seconds(0) == pytest.approx(0.0)
    assert domain.frame_to_seconds(2) == pytest.approx(0.10)  # 非线性 PTS
    assert domain.frame_to_seconds(4) == pytest.approx(0.25)


def test_b04_vfr_seconds_to_frame_nearest():
    domain = _domain(Rational(24, 1), total=5, index=_vfr_index())
    assert domain.seconds_to_frame(0.11) == 2   # 距 0.10 最近
    assert domain.seconds_to_frame(0.24) == 4
    assert domain.seconds_to_frame(0.0) == 0


def test_b04_cfr_fallback_without_index():
    domain = _domain(FPS_24, total=240)
    assert domain.frame_to_seconds(48) == pytest.approx(2.0)
    assert domain.seconds_to_frame(2.0) == 48


# ── In/Out：包含语义 + 全部拒绝原因 ────────────────────────────────


def test_b04_range_out_is_inclusive():
    domain = _domain(FPS_24, total=240)
    check = domain.validate_range(10, 49)
    assert check.ok
    assert check.length == 40                    # out − in + 1


def test_b04_range_in_equals_out_is_one_frame():
    domain = _domain(FPS_24, total=240)
    check = domain.validate_range(30, 30)
    assert check.ok
    assert check.length == 1                     # 一帧范围不得变零长度


def test_b04_range_single_frame_image():
    domain = _domain(FPS_24, total=1)
    check = domain.validate_range(0, 0)
    assert check.ok and check.length == 1


def test_b04_range_rejection_reasons():
    domain = _domain(FPS_24, total=240)
    assert domain.validate_range(None, None).reason == REASON_RANGE_EMPTY
    assert domain.validate_range(50, 10).reason == REASON_IN_AFTER_OUT
    assert domain.validate_range(-1, 10).reason == REASON_NEGATIVE
    assert domain.validate_range(0, -2).reason == REASON_NEGATIVE
    assert domain.validate_range(0, 240).reason == REASON_PAST_END
    assert domain.validate_range(240, 250).reason == REASON_PAST_END
    audio_only = _domain(FPS_24, total=0)
    assert audio_only.validate_range(0, 0).reason == REASON_NO_FRAMES


# ── 按资产状态存储：媒体版本校验 ───────────────────────────────────


def test_b04_state_store_roundtrip_and_invalidation():
    store = SourceMonitorStateStore()
    store.save("a1", "f1", 240, frame=100, in_point=10, out_point=50)

    restored = store.restore("a1", "f1", 240)
    assert restored is not None
    assert (restored.frame, restored.in_point, restored.out_point) == (
        100, 10, 50)

    # 媒体换版（指纹/帧数变化）→ 旧状态作废
    assert store.restore("a1", "f2", 240) is None
    assert store.restore("a1", "f1", 300) is None
    # 未知资产
    assert store.restore("nope", "f1", 240) is None


def test_b04_state_store_latest_wins_and_scoped():
    store = SourceMonitorStateStore()
    store.save("a1", "f1", 240, frame=10)
    store.save("a1", "f1", 240, frame=20, in_point=5, out_point=25)
    assert store.restore("a1", "f1", 240).frame == 20

    store.save("a2", "f2", 100, frame=1)
    assert store.restore("a2", "f2", 100).frame == 1
    assert store.restore("a1", "f1", 240).frame == 20   # 互不干扰

    # 恢复是只读的：重复 restore 结果一致且不改变存储
    first = store.restore("a1", "f1", 240)
    second = store.restore("a1", "f1", 240)
    assert first == second


def test_b04_state_store_bounded_lru():
    store = SourceMonitorStateStore(max_assets=2)
    store.save("a1", "f1", 10, frame=1)
    store.save("a2", "f2", 10, frame=2)
    store.save("a3", "f3", 10, frame=3)
    assert store.restore("a1", "f1", 10) is None   # 最旧被淘汰
    assert store.restore("a3", "f3", 10).frame == 3
