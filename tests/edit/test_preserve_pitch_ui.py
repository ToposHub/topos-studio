"""P1-08 preserve-pitch UI 开关测试。

模型层（SpeedParams）+ 属性面板 Speed section 接线：
- 所有 with_* 修改器必须携带 preserve_pitch（否则改一次速度就静默复位）。
- Speed section 渲染 preserve_pitch 布尔行，tooltip 标注"导出保调/预览变调"。
- 开关变化经 clip_param_changed 发出完整 SpeedParams。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.clip_params import SpeedParams


@pytest.mark.edit_core
class TestSpeedParamsPreservePitch:

    def test_with_preserve_pitch_sets_flag(self):
        params = SpeedParams(speed=1.5).with_preserve_pitch(True)
        assert params.preserve_pitch is True
        assert params.speed == 1.5

    def test_all_modifiers_carry_preserve_pitch(self):
        base = SpeedParams(speed=1.5, preserve_pitch=True)
        assert base.with_speed(2.0).preserve_pitch is True
        assert base.with_reverse(True).preserve_pitch is True
        assert base.with_interpolation("frame_blend").preserve_pitch is True
        pointed = base.with_speed_point(10, 100)
        assert pointed.preserve_pitch is True
        assert pointed.without_speed_points().preserve_pitch is True

    def test_serialization_round_trip(self):
        params = SpeedParams(speed=0.5, preserve_pitch=True)
        data = params.to_dict()
        assert data["preserve_pitch"] is True
        restored = SpeedParams.from_dict(data)
        assert restored.preserve_pitch is True


@pytest.mark.edit_ui
class TestSpeedSectionPreservePitchRow:

    def _make_clip(self, speed) -> SimpleNamespace:
        clip = SimpleNamespace(
            clip_id="c1", speed=speed, effects=(),
            timeline_start=0, timeline_end=100,
        )
        return clip

    def test_section_builds_preserve_pitch_widget(self, qapp):
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        panel = EditPropertiesPanel()
        clip = self._make_clip(SpeedParams(speed=1.5, preserve_pitch=True))
        panel._build_speed_section(clip, collapsed=False)
        section = panel._sections.get("clip:c1:speed")
        assert section is not None
        info = section._param_widgets.get("preserve_pitch")
        assert info is not None, "preserve_pitch 行缺失"
        assert info["widget"].isChecked() is True

    def test_emit_preserve_pitch_round_trip(self, qapp):
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        panel = EditPropertiesPanel()
        captured = []
        panel.clip_param_changed.connect(
            lambda cid, group, name, value: captured.append((cid, group, name, value)))
        clip = self._make_clip(SpeedParams(speed=1.5))
        panel._emit_speed_param(clip, "preserve_pitch", True)
        assert len(captured) == 1
        cid, group, name, value = captured[0]
        assert (cid, group, name) == ("c1", "speed", "preserve_pitch")
        assert isinstance(value, SpeedParams)
        assert value.preserve_pitch is True
        assert value.speed == 1.5

    def test_speed_change_keeps_preserve_pitch(self, qapp):
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        panel = EditPropertiesPanel()
        captured = []
        panel.clip_param_changed.connect(
            lambda cid, group, name, value: captured.append(value))
        clip = self._make_clip(SpeedParams(speed=1.5, preserve_pitch=True))
        panel._emit_speed_param(clip, "speed", 2.0)
        assert captured[0].preserve_pitch is True
        assert captured[0].speed == 2.0
