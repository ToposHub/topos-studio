"""
Audio Mixer 虚假入口禁用测试（AM-11 阶段 0.3）。

验证未实现的功能入口在功能完成前处于禁用态，
不会"可点击但仅记日志后伪装成功"。

验收标准（计划表 0.3）：
- UI 测试验证不可点击
- 不会仅记录日志后伪装成功
- tooltip/文案明确标注"未实现"

E17: Purge Cache 的死信号/开关/no-op 处理器已删除（全仓无 emit），
不再有占位入口需要禁用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.ui.audio_mixer.mixer_feature_flags import (
    RECORD_ARM_IMPLEMENTED,
)


class TestFeatureFlagsDefaults:
    """特性开关默认值。"""

    def test_record_arm_flag_implemented(self):
        """§4.4: Record Arm 已实现（recording_manager + UI 接线），开关为 True。"""
        assert RECORD_ARM_IMPLEMENTED is True

    def test_purge_cache_path_removed(self):
        """E17: Purge Cache 开关已随死路径删除，全仓无占位入口。"""
        import src.shared.ui.audio_mixer.mixer_feature_flags as flags
        assert not hasattr(flags, "PURGE_CACHE_IMPLEMENTED")


class TestRecordArmButtonState:
    """R 按钮（Record Arm）状态：§4.4 实现后启用。"""

    @pytest.fixture
    def mixer_widget(self, qapp):
        from src.shared.ui.audio_mixer import TrackMixerWidget
        return TrackMixerWidget("test-track-arm", "Test", "A1", is_master=False)

    def test_r_button_exists(self, mixer_widget):
        """R 按钮存在（非 Master 轨）。"""
        assert hasattr(mixer_widget, '_monitor_btn')
        assert mixer_widget._monitor_btn is not None

    def test_r_button_enabled_when_implemented(self, mixer_widget):
        """§4.4: 功能实现时 R 按钮启用，可点击录音。"""
        if RECORD_ARM_IMPLEMENTED:
            assert mixer_widget._monitor_btn.isEnabled() is True, (
                "Record Arm 已实现，R 按钮应启用"
            )

    def test_r_button_click_does_not_arm(self, qapp, mixer_widget):
        """§4.4: R 按钮初始 arm 状态为 False（未录音）。

        实际录音由 controller 接线（_on_mixer_record_arm_toggled），
        这里只验证 widget 初始状态。
        """
        # 初始未 arm
        assert mixer_widget._is_armed is False
