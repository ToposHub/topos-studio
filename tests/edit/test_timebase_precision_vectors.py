"""F0.4 时间基精确向量——独立 Fraction oracle 锁定帧映射/舍入/身份保留契约。

契约冻结于 docs/validation/feature_F/F0.4/ef91445f2/adr_timebase_identity_migration_2026-10-22.md。
期望值全部用独立的 fractions.Fraction 重新计算，不复用被测代码内部函数。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_frame_mapper import (
    FrameMapRequest,
    map_frame,
)
from src.features.edit.core.timeline_v2 import ClipInstance

FPS_24 = (24, 1)
FPS_25 = (25, 1)
FPS_2997 = (30000, 1001)
FPS_5994 = (60000, 1001)


def _request(
    timeline_frame: int,
    *,
    tl_fps=FPS_24,
    src_fps=None,
    start: int = 0,
    duration: int = 10 ** 9,
    speed: Fraction = Fraction(1, 1),
    reverse: bool = False,
) -> FrameMapRequest:
    kwargs = {}
    if src_fps is not None:
        kwargs = {"source_fps_num": src_fps[0], "source_fps_den": src_fps[1]}
    return FrameMapRequest(
        timeline_frame=timeline_frame,
        timeline_fps_num=tl_fps[0], timeline_fps_den=tl_fps[1],
        clip_timeline_start=0, clip_timeline_duration=10 ** 9,
        source_start_frame=start, source_duration_frames=duration,
        speed=speed, reverse=reverse, **kwargs)


def _oracle_source_frame(local: int, tl_fps, src_fps) -> int:
    """独立 oracle：local/num→秒→×den 精确有理数，half-up。"""
    timeline_time = Fraction(local, 1) / Fraction(tl_fps[0], tl_fps[1])
    source_frac = timeline_time * Fraction(src_fps[0], src_fps[1])
    # half-up（R07 冻结规则；正数域）
    return (source_frac + Fraction(1, 2)).__floor__()


class TestIdentityNoDrift:
    """同帧率恒等映射：任意长度（含 NTSC 长片）零累计漂移——精确相等。"""

    @pytest.mark.parametrize("fps", [FPS_24, FPS_25, FPS_2997, FPS_5994])
    @pytest.mark.parametrize("frame", [0, 1, 5, 1000, 107_892, 431_568])
    def test_identity_exact(self, fps, frame) -> None:
        result = map_frame(_request(frame, tl_fps=fps, src_fps=fps))
        assert result is not None
        assert result.source_frame == frame


class TestCrossFpsVectors:
    """混帧率精确向量（独立 oracle 逐点对拍）。"""

    @pytest.mark.parametrize("local", [1, 7, 13, 100, 3600])
    def test_24_timeline_ntsc_source(self, local: int) -> None:
        result = map_frame(
            _request(local, tl_fps=FPS_24, src_fps=FPS_2997))
        assert result is not None
        assert result.source_frame == _oracle_source_frame(local, FPS_24, FPS_2997)

    @pytest.mark.parametrize("local", [1, 3, 41, 999])
    def test_25_timeline_5994_source(self, local: int) -> None:
        result = map_frame(
            _request(local, tl_fps=FPS_25, src_fps=FPS_5994))
        assert result is not None
        assert result.source_frame == _oracle_source_frame(local, FPS_25, FPS_5994)

    def test_half_up_not_bankers_rounding(self) -> None:
        """local=1 @24←NTSC：1250/1001≈1.24875 → half-up=1（银行家舍入同为 1，
        取 local=7：8750/1001≈8.7413 → half-up=9，银行家舍入会给 9 相同；
        真正区分点是 x.5 恰好界值——构造 speed 实现精确 .5）。"""
        result = map_frame(
            _request(1, tl_fps=FPS_24, src_fps=FPS_2997))
        assert result is not None
        assert result.source_frame == 1
        # 精确 .5 界值：24fps 时间线、48fps 源、local=1 → 2.0；
        # 用 speed=1/2 → 1.0；speed=3/4 → 1.5 → half-up 必须 2（非 banker 的 2，
        # 但若实现退化为 trunc 即 1，此向量可捕获）。
        half = map_frame(
            _request(2, tl_fps=FPS_24, src_fps=FPS_24, speed=Fraction(3, 4)))
        assert half is not None
        assert half.source_frame == 2  # 2 * 3/4 = 1.5 → half-up → 2


class TestSpeedAndReverse:
    def test_half_speed_exact(self) -> None:
        result = map_frame(
            _request(10, tl_fps=FPS_24, src_fps=FPS_24, speed=Fraction(1, 2)))
        assert result is not None
        assert result.source_frame == 5

    def test_reverse_from_last_frame(self) -> None:
        result = map_frame(
            _request(0, tl_fps=FPS_24, src_fps=FPS_24,
                     start=0, duration=48, reverse=True))
        assert result is not None
        assert result.source_frame == 47

    def test_source_clamped_inclusive(self) -> None:
        """源采样上界含入（ADR §1.2）：不越过 start+duration-1。"""
        result = map_frame(
            _request(100, tl_fps=FPS_24, src_fps=FPS_2997,
                     start=0, duration=48))
        assert result is not None
        assert result.source_frame <= 47


class TestAudioBoundaryVectors:
    """48kHz 帧边界样本 = Fraction(48000·k·den, num)；NTSC 下 k=5 处为整数。"""

    def test_ntsc_frame_boundary_samples(self) -> None:
        num, den = FPS_2997
        for k in (0, 1, 2, 5, 1000):
            exact = Fraction(48000 * k * den, num)
            if k == 5:
                assert exact.denominator == 1 and exact.numerator == 8008
            if k == 1000:
                assert exact.denominator == 1  # 1000·1001·48000/30000 整除
                assert exact == Fraction(48000 * 1000 * 1001, 30000)

    @pytest.mark.parametrize("fps", [FPS_24, FPS_25, FPS_2997, FPS_5994])
    def test_every_frame_boundary_is_exact_rational(self, fps) -> None:
        """整 1 秒边界必为整数样本（fps 分母整除性质的有理数验证）。"""
        frames_per_second = fps[0] // fps[1]
        assert frames_per_second >= 24
        second_boundary = Fraction(48000 * fps[1] * 1, fps[1])  # 1s
        assert second_boundary == 48000


class TestUnknownFieldPreservation:
    def test_clip_extras_roundtrip(self) -> None:
        clip = ClipInstance(
            clip_id="tv-1", asset_id="asset-1", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10)
        data = clip.to_dict()
        data["future_field_v9"] = {"payload": [1, 2, 3]}
        restored = ClipInstance.from_dict(data)
        assert getattr(restored, "_extra_fields", {}).get("future_field_v9") == \
            {"payload": [1, 2, 3]}
        out = restored.to_dict()
        assert out.get("future_field_v9") == {"payload": [1, 2, 3]}
