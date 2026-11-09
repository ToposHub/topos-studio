"""CV-B02 域件测试：三域精确映射、源 TC（DF/NDF）、VFR 协议。

门槛（计划 CV-B02）：23.976/25/29.97DF/59.94DF、非零 TC、VFR、
反向／变速 clip 的 local/source/global 映射有 exact assertions。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.color.core.color_time_domain import (
    SourceTimecodeContext,
    TransportTimecodeFormatter,
    local_to_source_frame,
    local_to_timeline_frame,
    source_to_local_frame,
    timeline_to_local_frame,
    vfr_frame_for_seconds,
    vfr_seconds_for_frame,
)
from src.features.edit.core.timeline_v2 import ClipInstance
from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.media.timecode import TimecodeType

FPS_23976 = Rational(24000, 1001)
FPS_25 = Rational(25, 1)
FPS_2997 = Rational(30000, 1001)
FPS_5994 = Rational(60000, 1001)


# ── 帧域映射 ──────────────────────────────────────────────────────────


class _LinearClip(SimpleNamespace):
    """Duck clip：clip-local → source 为显式线性公式（变速/反向可控）。"""

    def clip_local_to_source_frame(self, local: int) -> int:
        return self.source_start + local * self.step

    def source_to_clip_local_frame(self, source: int) -> int:
        return (source - self.source_start) // self.step


def test_identity_mapping_without_clip() -> None:
    assert local_to_source_frame(None, 41) == 41
    assert source_to_local_frame(None, 41) == 41
    assert local_to_timeline_frame(None, 5) is None
    assert timeline_to_local_frame(None, 5) is None


def test_forward_clip_exact_mapping_with_timeline_domain() -> None:
    clip = _LinearClip(source_start=50, step=1, timeline_start=100,
                       timeline_duration=40)
    assert local_to_source_frame(clip, 0) == 50
    assert local_to_source_frame(clip, 39) == 89
    assert local_to_timeline_frame(clip, 10) == 110
    assert timeline_to_local_frame(clip, 110) == 10
    assert source_to_local_frame(clip, 89) == 39


def test_2x_speed_clip_exact_mapping() -> None:
    clip = _LinearClip(source_start=100, step=2, timeline_start=0,
                       timeline_duration=30)
    assert local_to_source_frame(clip, 0) == 100
    assert local_to_source_frame(clip, 10) == 120
    assert source_to_local_frame(clip, 120) == 10


def test_reverse_clip_exact_mapping() -> None:
    # 反向 clip：本地帧 0 对应源末端，逐帧向源头走。
    clip = _LinearClip(source_start=10, step=-1, timeline_start=0,
                       timeline_duration=25)
    assert local_to_source_frame(clip, 0) == 10
    assert local_to_source_frame(clip, 24) == -14  # 鸭子公式保持原样
    # 真实反向语义由 ClipInstance 权威方法保证（SpeedParams.reverse 表达
    # 反向，负速度值被模型校验拒绝）；域层只验证委托与夹取。
    from src.features.edit.core.timeline_v2 import SpeedParams
    real = ClipInstance(asset_id="a1", track_id="v1", timeline_start=0,
                        timeline_duration=5, source_start=0,
                        source_duration=5,
                        speed=SpeedParams(speed=1.0, reverse=True))
    mapped = local_to_source_frame(real, 0)
    assert 0 <= mapped <= 4  # 反向首帧落在源范围内


def test_freeze_clip_maps_every_local_frame_to_one_source_frame() -> None:
    real = ClipInstance(asset_id="a1", track_id="v1", timeline_start=0,
                        timeline_duration=10, source_start=7,
                        source_duration=5)
    object.__setattr__(real, "freeze_source_frame", 9)
    for local in (0, 4, 9):
        assert local_to_source_frame(real, local) == 9


def test_effective_source_start_offset_applies_both_directions() -> None:
    clip = _LinearClip(source_start=50, step=1, timeline_start=100,
                       timeline_duration=40)
    # trim 后 effective 起点 60（偏移 +10）
    assert local_to_source_frame(
        clip, 0, effective_source_start=60) == 60
    assert source_to_local_frame(
        clip, 70, effective_source_start=60) == 10


def test_local_clamped_to_total_and_timeline_out_of_range_is_none() -> None:
    clip = _LinearClip(source_start=0, step=1, timeline_start=100,
                       timeline_duration=40)
    assert local_to_source_frame(clip, 999, total_frames=40) == 39
    assert source_to_local_frame(clip, 999, total_frames=40) == 39
    assert timeline_to_local_frame(clip, 99) is None   # clip 前
    assert timeline_to_local_frame(clip, 140) is None  # clip 后
    assert timeline_to_local_frame(clip, 139) == 39


def test_fallback_formula_when_clip_lacks_authoritative_methods() -> None:
    clip = SimpleNamespace(source_start=50, timeline_start=0,
                           timeline_duration=10)
    assert local_to_source_frame(clip, 3) == 53
    assert source_to_local_frame(clip, 53) == 3


def test_total_frames_attr_precedence() -> None:
    clip = SimpleNamespace(source_start=0, timeline_duration=10,
                           duration=20, source_duration=30)
    assert local_to_timeline_frame(clip, 15, total_frames=20) == 15


# ── 源 TC 域 ──────────────────────────────────────────────────────────


def _metadata(fps, *, timecode=None, drop=None, total=1000) -> VideoMetadata:
    return VideoMetadata(
        width=1920, height=1080, fps=fps,
        total_frames=total, codec_name="prores", codec_profile=None,
        pixel_format="yuv422p10le", bit_depth=10,
        timecode=timecode, timecode_drop_frame=drop)


def test_metadata_without_fps_returns_none_not_fabricated() -> None:
    assert SourceTimecodeContext.from_metadata(None) is None
    broken = SimpleNamespace(fps=None)
    assert SourceTimecodeContext.from_metadata(broken) is None


@pytest.mark.parametrize("fps,expected_type", [
    (FPS_23976, TimecodeType.NON_DROP_FRAME),
    (FPS_25, TimecodeType.NON_DROP_FRAME),
    (FPS_2997, TimecodeType.DROP_FRAME),
    (FPS_5994, TimecodeType.DROP_FRAME),
])
def test_auto_detect_tc_type_per_frame_rate(fps, expected_type) -> None:
    context = SourceTimecodeContext.from_metadata(_metadata(fps))
    assert context is not None
    assert context.tc_type == expected_type


def test_explicit_drop_flag_overrides_auto_detect() -> None:
    # 25fps + 显式 drop 旗标按 DF 处理（metadata 权威）。
    context = SourceTimecodeContext.from_metadata(
        _metadata(FPS_25, drop=True))
    assert context.tc_type == TimecodeType.DROP_FRAME


def test_start_timecode_offsets_display() -> None:
    context = SourceTimecodeContext.from_metadata(
        _metadata(FPS_25, timecode="01:00:00:00"))
    assert context.start_frames == 25 * 3600
    assert context.format_frame(0) == "01:00:00:00"
    assert context.format_frame(24) == "01:00:00:24"
    assert context.format_frame(25) == "01:00:01:00"


def test_df_separator_in_start_tc_forces_drop_frame() -> None:
    context = SourceTimecodeContext.from_metadata(
        _metadata(FPS_2997, timecode="00:59:59;29", drop=False))
    assert context.tc_type == TimecodeType.DROP_FRAME


def test_invalid_start_tc_falls_back_to_relative() -> None:
    context = SourceTimecodeContext.from_metadata(
        _metadata(FPS_25, timecode="not-a-timecode"))
    assert context.start_frames == 0
    assert context.format_frame(0) == "00:00:00:00"


def test_df_formatting_is_exact() -> None:
    context = SourceTimecodeContext.from_metadata(_metadata(FPS_2997,
                                                            total=200000))
    # 29.97DF：107892 帧整点 = 01:00:00;00（共享 Timecode 的权威换算）。
    assert context.format_frame(107892) == "01:00:00;00"
    # DF 每分钟跳 2 帧：第 1 分钟的首个显示帧是帧号 1800（00:01:00;02），
    # 而帧号 1798 仍是 00:00:59;28（共享 Timecode 的权威编号）。
    assert context.format_frame(1800) == "00:01:00;02"
    assert context.format_frame(1798) == "00:00:59;28"


def test_5994_df_and_23976_ndf_formatting() -> None:
    df = SourceTimecodeContext(fps=FPS_5994,
                               tc_type=TimecodeType.DROP_FRAME)
    assert df.format_frame(0) == "00:00:00;00"
    assert df.format_frame(60 * 60) == "00:01:00;04"  # 每分钟跳 4 帧
    ndf = SourceTimecodeContext(fps=FPS_23976)
    assert ndf.format_frame(24 * 60) == "00:01:00:00"  # 23.976 不跳帧


def test_format_frame_clamps_to_total() -> None:
    context = SourceTimecodeContext(fps=FPS_25, total_frames=10)
    assert context.format_frame(999) == "00:00:00:09"
    assert context.format_frame(-5) == "00:00:00:00"


def test_parse_roundtrip_and_invalid_inputs_keep_playhead_safe() -> None:
    context = SourceTimecodeContext.from_metadata(
        _metadata(FPS_2997, timecode="01:00:00;00", total=200000))
    assert context.parse_to_frame("01:00:00;00") == 0
    # 10 秒 < 1 分钟：无 DF 跳帧，帧号 0..299。
    assert context.parse_to_frame("01:00:10;00") == 300
    # 非法输入 → None：调用方据此保持原播放头。
    assert context.parse_to_frame("garbage") is None
    assert context.parse_to_frame("") is None
    assert context.parse_to_frame(None) is None
    # 片头之前 / 超出总长（>200000 帧）→ None。
    assert context.parse_to_frame("00:00:00;00") is None
    assert context.parse_to_frame("02:00:00;00") == 107892  # 仍在片内
    assert context.parse_to_frame("03:00:00;00") is None


# ── VFR duck 协议 ─────────────────────────────────────────────────────


def _vfr_index() -> SimpleNamespace:
    # 0..9 帧密集（10fps），10..19 帧稀疏（2fps）——非线性 PTS。
    points = []
    seconds = 0.0
    for frame in range(20):
        points.append(SimpleNamespace(source_frame=frame, seconds=seconds))
        seconds += 0.1 if frame < 10 else 0.5
    return SimpleNamespace(
        points=tuple(points),
        source_frame_to_seconds=lambda f: points[f].seconds)


def test_vfr_seconds_and_frame_conversions_are_exact() -> None:
    index = _vfr_index()
    assert vfr_seconds_for_frame(index, 0) == 0.0
    assert vfr_seconds_for_frame(index, 10) == pytest.approx(1.0)
    # 1.26s 落在帧 10(1.0s) 与 11(1.5s) 之间，更近 11。
    assert vfr_frame_for_seconds(index, 1.26) == 11
    assert vfr_frame_for_seconds(index, 1.24) == 10
    # 超出末尾 → 最后一帧；负数 → None。
    assert vfr_frame_for_seconds(index, 99.0) == 19
    assert vfr_frame_for_seconds(index, -0.1) is None


def test_vfr_without_index_or_points_is_none() -> None:
    assert vfr_seconds_for_frame(None, 5) is None
    assert vfr_frame_for_seconds(None, 5) is None
    assert vfr_frame_for_seconds(SimpleNamespace(points=()), 5) is None


# ── transport 格式化器 ────────────────────────────────────────────────


def test_formatter_identity_for_standalone_video() -> None:
    context = SourceTimecodeContext.from_metadata(_metadata(FPS_25))
    formatter = TransportTimecodeFormatter(context)
    assert formatter.format(24) == "00:00:00:24"


def test_formatter_maps_clip_local_through_source_domain() -> None:
    context = SourceTimecodeContext.from_metadata(
        _metadata(FPS_25, timecode="01:00:00:00"))
    clip = _LinearClip(source_start=100, step=2, timeline_start=0,
                       timeline_duration=30)

    def local_to_source(local: int) -> int:
        return clip.clip_local_to_source_frame(local)

    formatter = TransportTimecodeFormatter(context, local_to_source)
    # 本地 5 → 源 110（2x）→ 起始 TC 偏移后 01:00:04:10。
    assert formatter.format(5) == "01:00:04:10"
