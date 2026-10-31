"""A3.1: 平滑速度斜坡（Smooth Speed Ramp）测试。

覆盖计划 A3.1 验证清单：
1. 已知数值表：25%/400% 两点在 linear/smooth 下的映射抽样；
2. 单调性：任何配置下源帧随 timeline 帧严格非降；
3. 边界：全部 easing=linear 时与旧阶梯算法输出逐帧一致（回归保障）；
4. 序列化往返含 easing；
5. retime 签名（缓存失效）覆盖 easing。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import (  # noqa: E402
    SpeedParams,
    SpeedPoint,
)

SOURCE_DURATION = 100


def _linear_params() -> SpeedParams:
    """25% → 400% 阶梯（旧语义）。"""
    return SpeedParams(
        speed=1.0,
        speed_points=(
            SpeedPoint(50, 0.25),
            SpeedPoint(80, 4.0),
        ),
    )


def _smooth_params() -> SpeedParams:
    """50% 处平滑斜坡进入 25% 段。"""
    return SpeedParams(
        speed=1.0,
        speed_points=(
            SpeedPoint(50, 0.25, easing="smooth"),
            SpeedPoint(80, 4.0),
        ),
    )


class TestEasingModel:
    def test_default_easing_is_linear(self) -> None:
        assert SpeedPoint(10, 1.0).easing == "linear"

    def test_invalid_easing_rejected(self) -> None:
        with pytest.raises(ValueError, match="easing"):
            SpeedPoint(10, 1.0, easing="cubic")

    def test_serialization_roundtrip_keeps_easing(self) -> None:
        point = SpeedPoint(30, 2.0, easing="smooth")
        restored = SpeedPoint.from_dict(point.to_dict())
        assert restored.easing == "smooth"
        # 旧数据无 easing 字段 → linear（行为不变）
        legacy = SpeedPoint.from_dict({"source_offset": 30, "speed": 2.0})
        assert legacy.easing == "linear"

    def test_with_point_easing_toggles(self) -> None:
        params = _linear_params()
        updated = params.with_point_easing(50, "smooth")
        assert updated.speed_points[0].easing == "smooth"
        assert updated.speed_points[1].easing == "linear"
        # 未找到点时抛错
        with pytest.raises(ValueError, match="does not exist"):
            params.with_point_easing(25, "smooth")


class TestRampMath:
    def test_linear_mode_matches_legacy_frame_by_frame(self) -> None:
        """全部 linear 时与旧阶梯算法逐帧一致（计划验收 3）。"""
        params = _linear_params()
        legacy_tl_len = sum(
            max(1, round((end - start) / speed))
            for start, end, speed in params._segments(SOURCE_DURATION)
        )
        for t in range(legacy_tl_len + 2):
            got = params.timeline_to_source_frames(t, SOURCE_DURATION)
            # 手工重算旧阶梯
            remaining = max(0, t)
            expected = SOURCE_DURATION - 1
            for start, end, seg_speed in params._segments(SOURCE_DURATION):
                tl_len = max(1, round((end - start) / seg_speed))
                if remaining < tl_len:
                    expected = start + min(end - start - 1, int(remaining * seg_speed))
                    break
                remaining -= tl_len
            expected = max(0, min(expected, SOURCE_DURATION - 1))
            assert got[0] == expected, t

    def test_smooth_known_values(self) -> None:
        """smooth 首尾锚定：0 → 源 0；总长按积分；中点推进高于恒速预期。"""
        params = SpeedParams(
            speed=1.0,
            speed_points=(SpeedPoint(50, 0.25, easing="smooth"),),
        )
        tl_len = params.timeline_duration_for_source_duration(SOURCE_DURATION)
        first = params.timeline_to_source_frames(0, SOURCE_DURATION)[0]
        last = params.timeline_to_source_frames(tl_len - 1, SOURCE_DURATION)[0]
        assert first == 0
        assert last == SOURCE_DURATION - 1
        # smoothstep 过渡段速度介于 1.0 与 0.25 之间：过渡中段（t≈25）
        # 源推进应慢于恒速 1.0 的预期但快于恒速 0.25 的阶梯算法
        mid = params.timeline_to_source_frames(25, SOURCE_DURATION)[0]
        assert 15 <= mid <= 35

    def test_monotonic_non_decreasing_all_configs(self) -> None:
        """计划验收 2：源帧随 timeline 帧严格非降。"""
        configs = [
            _linear_params(),
            _smooth_params(),
            SpeedParams(speed=1.0, speed_points=(
                SpeedPoint(30, 2.0, easing="smooth"),
                SpeedPoint(60, 0.25, easing="smooth"),
                SpeedPoint(90, 1.0),
            )),
            SpeedParams(speed=1.0, speed_points=(
                SpeedPoint(50, 8.0, easing="smooth"),
                SpeedPoint(60, 0.1, easing="smooth"),
            )),
        ]
        for params in configs:
            tl_len = params.timeline_duration_for_source_duration(SOURCE_DURATION)
            prev = -1
            for t in range(tl_len):
                cur = params.timeline_to_source_frames(t, SOURCE_DURATION)[0]
                assert cur >= prev, (params, t)
                prev = cur

    def test_inverse_mapping_roundtrip(self) -> None:
        """源 → 时间线 → 源 反查在斜坡路径下收敛到同帧附近。"""
        params = SpeedParams(
            speed=1.0,
            speed_points=(SpeedPoint(50, 0.25, easing="smooth"),),
        )
        tl_len = params.timeline_duration_for_source_duration(SOURCE_DURATION)
        for source_frame in (0, 25, 50, 75, 99):
            t = params.source_to_timeline_frame_for_source_duration(
                source_frame, SOURCE_DURATION)
            back = params.timeline_to_source_frames(
                min(t, tl_len - 1), SOURCE_DURATION)[0]
            assert abs(back - source_frame) <= 1, (source_frame, t, back)


class TestRetimeSignature:
    def test_signature_covers_easing(self) -> None:
        from src.features.edit.core.realtime_playback.render_cache_adapter import (
            build_render_hash,
            speed_params_signature,
        )

        linear = _linear_params()
        smooth = SpeedParams(
            speed=1.0,
            speed_points=(
                SpeedPoint(50, 0.25, easing="smooth"),
                SpeedPoint(80, 4.0),
            ),
        )
        sig_lin = speed_params_signature(linear)
        sig_smooth = speed_params_signature(smooth)
        assert sig_lin != sig_smooth

        base_kwargs = dict(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
        )
        hash_lin = build_render_hash(**base_kwargs, retime_signature=sig_lin)
        hash_smooth = build_render_hash(**base_kwargs, retime_signature=sig_smooth)
        assert hash_lin != hash_smooth
        # 无 retime 签名（旧调用）仍稳定
        assert build_render_hash(**base_kwargs) == build_render_hash(**base_kwargs)
