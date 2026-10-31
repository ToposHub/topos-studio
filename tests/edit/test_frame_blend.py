"""A3.2: 帧混合变速（Frame Blend）测试。

覆盖计划 A3.2 验证清单：
- pair API 数值表：慢速交替权重、2x 整速权重恒 0、1x 权重恒 0；
- 权重 ∈ [0,1]；
- map_frame_pair 与 SpeedParams 层一致；
- CPU lerp 公式（_apply_frame_blend_layer）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import (  # noqa: E402
    SpeedParams,
    SpeedPoint,
)
from src.features.edit.core.timeline_v2 import ClipInstance  # noqa: E402
from src.features.edit.core.timeline_frame_mapper import (  # noqa: E402
    FrameMapRequest,
    map_frame_pair,
)

FPS_NUM, FPS_DEN = 24, 1


from fractions import Fraction


def _request(timeline_frame: int, speed: float) -> FrameMapRequest:
    return FrameMapRequest(
        timeline_frame=timeline_frame,
        timeline_fps_num=FPS_NUM, timeline_fps_den=FPS_DEN,
        clip_timeline_start=0, clip_timeline_duration=200,
        source_start_frame=0, source_duration_frames=400,
        speed=Fraction(speed).limit_denominator(10000),
    )


class TestMapFramePair:
    def test_half_speed_alternating_weights(self) -> None:
        """0.5x：精确源位置 0.5t → 权重交替 0 / 0.5。"""
        for t in range(0, 6):
            req = _request(t, 0.5)
            ra, rb, w = map_frame_pair(req)
            assert ra.source_frame == t // 2
            assert rb.source_frame == ra.source_frame + 1
            assert 0.0 <= w <= 1.0
            expected_w = (t % 2) * 0.5
            assert w == pytest.approx(expected_w, abs=1e-6)

    def test_double_speed_weight_zero_on_even_frames(self) -> None:
        """2x 整速：整数帧边界对齐 → 权重 0（验收：整速段权重恒 0）。"""
        for t in range(0, 10):
            req = _request(t, 2.0)
            _ra, _rb, w = map_frame_pair(req)
            assert w == pytest.approx(0.0, abs=1e-9)

    def test_normal_speed_weight_zero(self) -> None:
        req = _request(7, 1.0)
        ra, rb, w = map_frame_pair(req)
        assert ra.source_frame == 7
        assert w == pytest.approx(0.0, abs=1e-9)

    def test_out_of_range_returns_none(self) -> None:
        req = _request(500, 1.0)
        assert map_frame_pair(req) is None


class TestSpeedParamsPair:
    def test_half_speed_pair_table(self) -> None:
        params = SpeedParams(speed=0.5)
        for t, (a, b, w) in (
            (0, (0, 1, 0.0)),
            (1, (0, 1, 0.5)),
            (2, (1, 2, 0.0)),
            (3, (1, 2, 0.5)),
        ):
            got = params.timeline_to_source_frame_pair(t, 400)
            assert (a, b) == (got[0], got[1]), t
            assert got[2] == pytest.approx(w, abs=1e-6)

    def test_unit_speed_weight_always_zero(self) -> None:
        params = SpeedParams(speed=1.0)
        for t in range(0, 50):
            _a, _b, w = params.timeline_to_source_frame_pair(t, 400)
            assert w == pytest.approx(0.0, abs=1e-9)

    def test_clamped_to_source_range(self) -> None:
        params = SpeedParams(speed=2.0)
        a, b, _w = params.timeline_to_source_frame_pair(199, 200)
        assert b <= 199 and a <= 199


class TestFrameBlendLerp:
    def test_lerp_applies_weight(self) -> None:
        from src.features.edit.core.timeline_frame_mapper import (
            _apply_frame_blend_layer,
        )
        from src.features.edit.core.timeline_render_plan import ClipLayerInfo

        speed = SpeedParams(speed=0.5, interpolation="frame_blend")
        clip = ClipInstance(
            clip_id="c", asset_id="a", track_id="v1",
            timeline_start=0, timeline_duration=8,
            source_start=0, source_duration=400,
            speed=speed,
        )
        layer = ClipLayerInfo(
            clip=clip, source_frame=0, track_index=0, track_name="V1",
            opacity=1.0, transform=(), crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        )
        calls = []

        def fake_get(l):
            calls.append(l.source_frame)
            return np.full((4, 4, 3), l.source_frame % 256, dtype=np.uint8)

        # t=1 → (0, 1, 0.5)：层帧(灰度0)与帧1(灰度1)各半 → 半上取整 1
        layer_frame = np.full((4, 4, 3), 0, dtype=np.uint8)
        out = _apply_frame_blend_layer(layer, layer_frame, 1, fake_get)
        assert out.mean() == pytest.approx(1.0, abs=0.5)
        assert calls == [1]

    def test_nearest_passthrough(self) -> None:
        from src.features.edit.core.timeline_frame_mapper import (
            _apply_frame_blend_layer,
        )
        from src.features.edit.core.timeline_render_plan import ClipLayerInfo

        speed = SpeedParams(speed=0.5)  # nearest
        clip = ClipInstance(
            clip_id="c", asset_id="a", track_id="v1",
            timeline_start=0, timeline_duration=8,
            source_start=0, source_duration=400,
            speed=speed,
        )
        layer = ClipLayerInfo(
            clip=clip, source_frame=0, track_index=0, track_name="V1",
            opacity=1.0, transform=(), crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        )
        layer_frame = np.full((4, 4, 3), 7, dtype=np.uint8)
        out = _apply_frame_blend_layer(
            layer, layer_frame, 3, lambda l: None)
        assert out is layer_frame  # nearest 不解码第二帧
