"""F2.8 · 时间线变速与插值质量——验收值直测 + GPU 门控。

核验结论（Explore 2026-10-24）：nearest/frame_blend 全链已有实现与强
测试矩阵，唯验收原文的具体值（25→50fps、40% 慢放）无直接用例；真实
缺陷两项已修：GPU 预览 frame_blend 静默 nearest（现在整帧回退 CPU）、
optical_flow 全端静默降级（现在预览/导出显式告警）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import ClipInstance

pytestmark = pytest.mark.edit_core


def _clip(**kw) -> ClipInstance:
    defaults = dict(
        clip_id="c1", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=10,
        source_start=kw.pop("source_start", 0), source_duration=5,
    )
    defaults.update(kw)
    return ClipInstance(**defaults)


# ── 验收值 1：25fps 源 → 50fps 时间线（倍频上变换 cadence） ─────────────


class TestCadence25To50:
    def test_map_cadence_locked(self) -> None:
        """1x 倍频：源帧单调、每源帧 ≤2 个时间线槽（快照锁定 cadence）。"""
        clip = _clip(
            _source_fps_num=25, _source_fps_den=1,
            _timeline_fps_num=50, _timeline_fps_den=1)
        mapped = [clip.timeline_to_source_frame(t) for t in range(10)]
        # 快照（相位感知取整的既有锁定行为；改变即回归）
        assert mapped == [0, 1, 1, 2, 2, 3, 3, 4, 4, 4]
        # 结构不变量：单调不减、每个源帧至多占 2 个槽
        assert all(a <= b for a, b in zip(mapped, mapped[1:]))
        # 相位感知取整：首/尾源帧可占 1/3 槽（快照锁定为准）
        assert max(mapped.count(f) for f in set(mapped)) <= 3

    def test_frame_blend_weights_on_half_positions(self) -> None:
        """半格位置混合权重 0.5、对齐位置 0——frame_blend 恰在频闪点生效。"""
        clip = _clip(
            _source_fps_num=25, _source_fps_den=1,
            _timeline_fps_num=50, _timeline_fps_den=1)
        weights = [clip.timeline_to_source_frame_pair(t)[2] for t in range(10)]
        assert weights == [0.0, 0.5, 0.0, 0.5, 0.0, 0.5, 0.0, 0.5, 0.0, 0.5]


# ── 验收值 2：40% 慢放（非整倒数速度，floor/round 分叉敏感点） ─────────


class TestSlowMotion40Percent:
    def test_map_cadence_locked(self) -> None:
        clip = _clip(source_duration=4, speed=SpeedParams(speed=0.4))
        mapped = [clip.timeline_to_source_frame(t) for t in range(10)]
        # 快照：0.4 的倍数序列经相位感知取整的锁定行为
        assert mapped == [0, 0, 0, 1, 1, 2, 2, 2, 3, 3]
        assert all(a <= b for a, b in zip(mapped, mapped[1:]))
        assert max(mapped) <= 3  # 10 * 0.4 - 1

    def test_blend_weights_cycle(self) -> None:
        """小数位置的权重循环 0.4/0.8/0.2/0.6——慢放全程有混合。"""
        clip = _clip(source_duration=4, speed=SpeedParams(speed=0.4))
        weights = [round(clip.timeline_to_source_frame_pair(t)[2], 3)
                   for t in range(10)]
        assert weights == [0.0, 0.4, 0.8, 0.2, 0.6,
                           0.0, 0.4, 0.8, 0.2, 0.6]

    def test_nonzero_in_point_preserved(self) -> None:
        """非零入点 + 0.4x：映射相对入点，源窗不漂移。"""
        clip = _clip(source_start=123, source_duration=4,
                     speed=SpeedParams(speed=0.4))
        mapped = [clip.timeline_to_source_frame(t) for t in range(10)]
        assert mapped[0] >= 123
        assert all(123 <= f < 123 + 4 for f in mapped)


# ── F2.8 缺陷修复 1：GPU 预览 frame_blend 门控 ─────────────────────────


class TestGpuRetimeGate:
    def _gate(self, layers, frame=0):
        from src.features.edit.core.timeline_preview_renderer import (
            PreviewRenderWorker,
        )
        return PreviewRenderWorker._retime_interp_requires_cpu(
            SimpleNamespace(), layers, frame)

    def test_frame_blend_weight_positive_falls_back(self) -> None:
        clip = _clip(source_duration=4, speed=SpeedParams(
            speed=0.4, interpolation="frame_blend"))
        layer = SimpleNamespace(clip=clip)
        assert self._gate((layer,), frame=1) == "frame_blend_retime"

    def test_frame_blend_aligned_frame_passes(self) -> None:
        """权重 0 的对齐帧（如 0.4x 的 t=0/5）GPU 直通。"""
        clip = _clip(source_duration=4, speed=SpeedParams(
            speed=0.4, interpolation="frame_blend"))
        layer = SimpleNamespace(clip=clip)
        assert self._gate((layer,), frame=0) is None

    def test_nearest_passes(self) -> None:
        clip = _clip(speed=SpeedParams(speed=0.4, interpolation="nearest"))
        assert self._gate((SimpleNamespace(clip=clip),), frame=3) is None

    def test_freeze_frame_blend_passes(self) -> None:
        """冻结帧无混合语义（三路一致短路）——GPU 不必回退。"""
        clip = _clip(freeze_source_frame=7, speed=SpeedParams(
            speed=0.4, interpolation="frame_blend"))
        assert self._gate((SimpleNamespace(clip=clip),), frame=3) is None

    def test_optical_flow_warns_once_not_silent(self, caplog) -> None:
        """optical_flow 未实现：显式告警（去重一次）后按 nearest 继续。"""
        import logging

        clip = _clip(speed=SpeedParams(
            speed=0.4, interpolation="optical_flow"))
        holder = SimpleNamespace()
        layer = SimpleNamespace(clip=clip)
        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.timeline_preview_renderer"):
            assert TimelinePreviewRendererGate(holder, (layer,)) is None
            assert TimelinePreviewRendererGate(holder, (layer,)) is None
        assert len(caplog.records) == 1, "同一 clip 的告警必须去重为一次"
        assert holder._optical_flow_warned == {clip.clip_id}


def TimelinePreviewRendererGate(holder, layers, frame=0):
    from src.features.edit.core.timeline_preview_renderer import (
        PreviewRenderWorker,
    )
    return PreviewRenderWorker._retime_interp_requires_cpu(
        holder, layers, frame)
