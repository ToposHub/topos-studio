"""A1: 时间线片段混合模式（Blend Modes）测试。

覆盖计划 A1 验证清单：
1. 16 种模式在固定像素对上的 CPU 公式结果（黑白/50%灰/彩三组期望值）；
2. SetClipBlendModeCommand 多选批量 + 撤销/序列化往返；
3. from_dict 无 blend_mode 字段的旧 dict → NORMAL（旧工程兼容）；
4. 三路同源：GPU shader 源包含 blend_channel 且与 NumPy 共享编号；
   CPU 合成路径（_composite_frame_into_buffer）按模式分派。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.blend_modes import (  # noqa: E402
    BLEND_MODE_CODES,
    blend_rgb,
)
from src.features.edit.core.clip_params import ClipBlendMode  # noqa: E402
from src.features.edit.core.commands import (  # noqa: E402
    SetClipBlendModeCommand,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")


def _px(s, d):
    """标量对 → (1,1,3) float32 数组。"""
    return (
        np.array([[[d]]], dtype=np.float32),
        np.array([[[s]]], dtype=np.float32),
    )


def _blend_one(s: float, d: float, mode) -> float:
    """单值对广播到 3 通道（HSL 模式需要 RGB），取 R 通道结果。"""
    base = np.full((1, 1, 3), d, dtype=np.float32)
    overlay = np.full((1, 1, 3), s, dtype=np.float32)
    out = blend_rgb(base, overlay, mode)
    return float(out[0, 0, 0])


class TestClipBlendModeEnum:
    def test_sixteen_modes_with_stable_codes(self) -> None:
        modes = list(ClipBlendMode)
        assert len(modes) == 16
        assert modes[0] == ClipBlendMode.NORMAL
        assert [m.code for m in modes] == list(range(16))

    def test_codes_table_matches_enum(self) -> None:
        assert BLEND_MODE_CODES["multiply"] == 1
        assert BLEND_MODE_CODES["luminosity"] == 15


class TestBlendFormulas:
    """计划验证 1：固定像素对上的公式期望值（±1/255 容差）。"""

    TOL = 1.5 / 255.0

    def test_normal_returns_overlay(self) -> None:
        assert _blend_one(0.6, 0.3, ClipBlendMode.NORMAL) == pytest.approx(0.6, abs=self.TOL)

    def test_black_white_gray_value_table(self) -> None:
        pairs = [(0.0, 1.0), (1.0, 0.0), (0.5, 0.5), (0.5, 0.25)]
        for s, d in pairs:
            assert _blend_one(s, d, "multiply") == pytest.approx(s * d, abs=self.TOL)
            assert _blend_one(s, d, "screen") == pytest.approx(
                s + d - s * d, abs=self.TOL)
            assert _blend_one(s, d, "darken") == pytest.approx(min(s, d), abs=self.TOL)
            assert _blend_one(s, d, "lighten") == pytest.approx(max(s, d), abs=self.TOL)
            assert _blend_one(s, d, "difference") == pytest.approx(
                abs(d - s), abs=self.TOL)
            assert _blend_one(s, d, "exclusion") == pytest.approx(
                s + d - 2 * s * d, abs=self.TOL)

    def test_overlay_hard_light_branches(self) -> None:
        # overlay 以底图 d 判断分支
        assert _blend_one(0.8, 0.3, "overlay") == pytest.approx(
            2 * 0.8 * 0.3, abs=self.TOL)
        assert _blend_one(0.8, 0.7, "overlay") == pytest.approx(
            1 - 2 * 0.2 * 0.3, abs=self.TOL)
        # hard_light 以源 s 判断分支
        assert _blend_one(0.3, 0.8, "hard_light") == pytest.approx(
            2 * 0.3 * 0.8, abs=self.TOL)
        assert _blend_one(0.7, 0.2, "hard_light") == pytest.approx(
            1 - 2 * 0.3 * 0.8, abs=self.TOL)

    def test_dodge_burn_boundaries(self) -> None:
        assert _blend_one(1.0, 0.3, "color_dodge") == pytest.approx(1.0, abs=self.TOL)
        assert _blend_one(0.0, 0.3, "color_burn") == pytest.approx(0.0, abs=self.TOL)
        # 中间值：dodge = min(1, d/(1-s))
        assert _blend_one(0.5, 0.3, "color_dodge") == pytest.approx(0.6, abs=self.TOL)

    def test_hsl_group_preserves_luminosity_or_hue(self) -> None:
        # color: 结果亮度 = 底图亮度
        base = np.full((1, 1, 3), 0.3, dtype=np.float32)
        overlay = np.full((1, 1, 3), 0.8, dtype=np.float32)
        out = blend_rgb(base, overlay, "color")
        expected_lum = 0.3 * 0.3 + 0.59 * 0.3 + 0.11 * 0.3
        actual_lum = float((0.3 * out[0, 0, 0] + 0.59 * out[0, 0, 1] + 0.11 * out[0, 0, 2]))
        assert actual_lum == pytest.approx(expected_lum, abs=0.02)
        # luminosity: 结果取底图色度 + 源亮度 → 全通道等于源亮度（灰度源）
        assert _blend_one(0.5, 0.5, "luminosity") == pytest.approx(0.5, abs=0.02)

    def test_all_modes_output_in_range(self) -> None:
        for mode in ClipBlendMode:
            for s, d in [(0.1, 0.9), (0.9, 0.1), (0.5, 0.5)]:
                value = _blend_one(s, d, mode)
                assert 0.0 <= value <= 1.0, (mode, s, d, value)


class TestBlendModeCommand:
    def _make_sequence(self) -> TimelineSequence:
        clips = (
            ClipInstance(clip_id="c1", asset_id="a", track_id="v1",
                         timeline_start=0, timeline_duration=24,
                         source_start=0, source_duration=24),
            ClipInstance(clip_id="c2", asset_id="a", track_id="v1",
                         timeline_start=24, timeline_duration=24,
                         source_start=24, source_duration=24),
        )
        track = EditTrack(track_id="v1", name="V1", track_type="video",
                          index=0, clips=clips)
        return TimelineSequence(video_tracks=(track,), fps=Rational(24, 1))

    def test_batch_apply_and_undo(self) -> None:
        seq = self._make_sequence()
        cmd = SetClipBlendModeCommand.create(["c1", "c2"], "multiply")
        new_seq = cmd.apply(seq)
        for cid in ("c1", "c2"):
            clip = new_seq.get_clip_by_id(cid)
            assert clip.blend_mode == ClipBlendMode.MULTIPLY
        undone = cmd.undo(new_seq)
        for cid in ("c1", "c2"):
            clip = undone.get_clip_by_id(cid)
            assert clip.blend_mode == ClipBlendMode.NORMAL

    def test_serialization_roundtrip(self) -> None:
        seq = self._make_sequence()
        cmd = SetClipBlendModeCommand.create(["c1"], ClipBlendMode.SCREEN)
        applied = cmd.apply(seq)
        restored = SetClipBlendModeCommand.from_dict(cmd.to_dict())
        assert restored.blend_mode == ClipBlendMode.SCREEN
        undone = restored.undo(applied)
        assert undone.get_clip_by_id("c1").blend_mode == ClipBlendMode.NORMAL


class TestModelCompat:
    def test_old_project_dict_defaults_to_normal(self) -> None:
        data = {
            "clip_id": "c9", "asset_id": "a", "track_id": "v1",
            "timeline_start": 0, "timeline_duration": 10,
            "source_start": 0, "source_duration": 10,
        }
        clip = ClipInstance.from_dict(data)
        assert clip.blend_mode == ClipBlendMode.NORMAL

    def test_invalid_string_falls_back_to_normal(self) -> None:
        clip = ClipInstance(
            clip_id="c9", asset_id="a", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10, blend_mode="junk",
        )
        assert clip.blend_mode == ClipBlendMode.NORMAL


class TestCpuCompositeDispatch:
    def test_blend_frames_u8_differs_from_normal(self, qapp) -> None:
        """共享入口：multiply 混合比 normal 结果暗（公式生效）。"""
        from src.features.edit.core.blend_modes import blend_frames_u8

        base = np.full((36, 64, 3), 200, dtype=np.uint8)
        overlay = np.full((36, 64, 3), 120, dtype=np.uint8)

        normal = blend_frames_u8(base, overlay, "normal", 1.0)
        multiply = blend_frames_u8(base, overlay, "multiply", 1.0)
        assert normal.max() == 120
        assert multiply.max() < 130  # ≈ 200*120/255 ≈ 94
        assert not np.array_equal(normal, multiply)

    def test_composite_method_wires_blend_mode(self) -> None:
        """CPU 合成方法签名/分派包含 blend_mode（静态守护三路同源）。"""
        import inspect

        from src.features.edit.core.timeline_preview_renderer import (
            PreviewRenderWorker,
        )

        source = inspect.getsource(
            PreviewRenderWorker._composite_frame_into_buffer)
        assert "blend_mode" in source
        assert "blend_frames_u8" in source


class TestGpuShaderSource:
    def test_blend_glsl_present_and_modes_covered(self) -> None:
        """三路同源：GLSL 公式文件存在且编号分支与 NumPy 共享表一致。"""
        import os

        from src.features.edit.core import blend_shader

        path = blend_shader._BLEND_PROGRAM_PATH
        assert os.path.exists(path)
        source = blend_shader._BLEND_FRAGMENT_SHADER_SOURCE
        # 16 个编号分支全部出现在 shader 中（mode == 1..15 + normal fallthrough）
        for code in range(1, 16):
            assert f"mode == {code}" in source
        assert "blend_channel" in source
        assert "u_blend_mode" in source

    def test_export_renderer_uses_shared_formulas(self) -> None:
        import src.features.edit.core.timeline_gpu_export_renderer as ex

        assert "blend_channel" in ex._FRAG_BLEND_F1_SRC
        assert "blend_channel" in ex._FRAG_BLEND_U16_SRC
        assert "blend_channel" in ex._FRAG_BLEND_F4_SRC
        assert callable(ex._BLEND_GLSL) is False  # 已在导入期读取为字符串
        assert "blend_set_lum" in ex._BLEND_GLSL
