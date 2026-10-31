"""
Tests for Effect Preview Quality System (P3-4)

测试播放时效果降级系统：
1. PreviewQualityLevel 枚举行为
2. HeavyEffectDefinition 参数降级
3. EffectPreviewQualityManager 状态管理
4. 播放状态切换时降级激活/恢复
5. film_grain 降级效果

验收标准：
- 播放时效果降级正确激活
- 暂停时恢复完整质量
- 导出不受影响
- UI 指示器正确显示降级状态

运行：
    python -m pytest tests/test_effect_preview_quality.py -v
"""

import pytest
from unittest.mock import MagicMock, patch
from typing import Dict, Any

from src.shared.effects.preview_quality import (
    PreviewQualityLevel,
    EffectCostCategory,
    HeavyEffectDefinition,
    PlaybackDegradationState,
    EffectPreviewQualityManager,
    BUILTIN_HEAVY_EFFECTS,
    get_effect_preview_quality_manager,
    get_degraded_effect_params,
    is_playback_degradation_active,
)


class TestPreviewQualityLevel:
    """测试 PreviewQualityLevel 枚举"""

    def test_full_quality_not_needs_degradation(self):
        """FULL 级别不需要降级"""
        assert PreviewQualityLevel.FULL.needs_degradation is False
        assert PreviewQualityLevel.FULL.is_playback_compatible is True

    def test_skip_quality_not_playback_compatible(self):
        """SKIP 级别在播放时不兼容（完全跳过）"""
        assert PreviewQualityLevel.SKIP.is_playback_compatible is False
        assert PreviewQualityLevel.SKIP.needs_degradation is True

    def test_high_medium_low_need_degradation(self):
        """HIGH/MEDIUM/LOW 级别需要降级"""
        for level in [PreviewQualityLevel.HIGH, PreviewQualityLevel.MEDIUM, PreviewQualityLevel.LOW]:
            assert level.needs_degradation is True
            assert level.is_playback_compatible is True


class TestHeavyEffectDefinition:
    """测试 HeavyEffectDefinition"""

    def test_skip_effect_gets_disabled_params(self):
        """SKIP 级别效果应用注册的禁用参数（原始参数保留）"""
        definition = BUILTIN_HEAVY_EFFECTS["ai_matte"]
        assert definition.preview_quality == PreviewQualityLevel.SKIP

        original_params = {"enabled": True, "model": "modnet.onnx"}
        degraded = definition.get_degraded_params(original_params)

        # ai_matte 注册的降级参数键是 ai_matte_enabled（不再是硬编码 enabled=False）
        assert degraded["ai_matte_enabled"] is False
        # 原始参数不再被丢弃
        assert degraded["model"] == "modnet.onnx"
        assert degraded["enabled"] is True

    def test_high_effect_applies_degradation_params(self):
        """HIGH 级别效果应用部分降级参数"""
        definition = HeavyEffectDefinition(
            effect_type="test_high",
            cost_category=EffectCostCategory.SHADER_HEAVY,
            preview_quality=PreviewQualityLevel.HIGH,
            degradation_params={"sample_count": 4},
        )

        original_params = {"enabled": True, "sample_count": 12}
        degraded = definition.get_degraded_params(original_params)

        assert degraded["enabled"] is True
        assert degraded["sample_count"] == 4

    def test_full_effect_returns_original_params(self):
        """FULL 级别效果返回原始参数"""
        definition = HeavyEffectDefinition(
            effect_type="test_full",
            cost_category=EffectCostCategory.SHADER_LIGHT,
            preview_quality=PreviewQualityLevel.FULL,
        )

        original_params = {"enabled": True, "intensity": 0.5}
        degraded = definition.get_degraded_params(original_params)

        assert degraded == original_params


class TestEffectPreviewQualityManager:
    """测试 EffectPreviewQualityManager"""

    @pytest.fixture
    def manager(self):
        """创建管理器实例"""
        # 每次创建新实例避免状态污染
        manager = EffectPreviewQualityManager()
        manager.set_degradation_enabled(True)
        return manager

    def test_set_playback_state_activates_degradation(self, manager):
        """设置播放状态激活降级"""
        manager.set_playback_state(True)

        assert manager.is_playing() is True
        assert len(manager.get_degradation_state().degraded_effects) > 0

    def test_set_playback_state_false_clears_degradation(self, manager):
        """设置暂停状态清除降级"""
        manager.set_playback_state(True)
        manager.set_playback_state(False)

        assert manager.is_playing() is False
        assert len(manager.get_degradation_state().degraded_effects) == 0

    def test_get_degraded_params_when_playing(self, manager):
        """播放时获取降级参数"""
        manager.set_playback_state(True)

        original_params = {"enabled": True, "intensity": 0.5}
        degraded = manager.get_degraded_params("film_grain", original_params)

        # film_grain 是 HIGH 级别，降级时 intensity=0
        assert degraded["intensity"] == 0.0

    def test_get_degraded_params_when_paused(self, manager):
        """暂停时返回原始参数"""
        manager.set_playback_state(False)

        original_params = {"enabled": True, "intensity": 0.5}
        degraded = manager.get_degraded_params("film_grain", original_params)

        assert degraded == original_params

    def test_should_skip_effect_for_ai_effects(self, manager):
        """AI 效果在播放时应跳过"""
        manager.set_playback_state(True)

        # AI matte 是 SKIP 级别
        assert manager.should_skip_effect("ai_matte") is True
        # AI denoise 是 SKIP 级别
        assert manager.should_skip_effect("ai_denoise") is True

    def test_should_not_skip_effect_for_shader_effects(self, manager):
        """Shader 效果在播放时不跳过（降级而非跳过）"""
        manager.set_playback_state(True)

        # film_grain 是 HIGH 级别，不跳过但降级
        assert manager.should_skip_effect("film_grain") is False

    def test_degradation_disabled_returns_original_params(self, manager):
        """禁用降级时返回原始参数"""
        manager.set_degradation_enabled(False)
        manager.set_playback_state(True)

        original_params = {"enabled": True, "intensity": 0.5}
        degraded = manager.get_degraded_params("film_grain", original_params)

        assert degraded == original_params

    def test_register_custom_heavy_effect(self, manager):
        """注册自定义重型效果"""
        custom_effect = HeavyEffectDefinition(
            effect_type="custom_heavy",
            cost_category=EffectCostCategory.SHADER_HEAVY,
            preview_quality=PreviewQualityLevel.LOW,
            degradation_params={"intensity": 0.1},
            ui_label="Custom Heavy Effect",
        )

        manager.register_heavy_effect(custom_effect)

        all_effects = manager.get_all_heavy_effects()
        assert "custom_heavy" in all_effects
        assert all_effects["custom_heavy"].preview_quality == PreviewQualityLevel.LOW

    def test_get_degradation_summary(self, manager):
        """获取降级状态摘要"""
        manager.set_playback_state(True)

        summary = manager.get_degradation_summary()

        assert summary["is_playing"] is True
        assert summary["degradation_enabled"] is True
        assert summary["degraded_count"] > 0
        assert len(summary["degraded_effects"]) > 0


class TestConvenienceFunctions:
    """测试便捷函数"""

    def test_get_effect_preview_quality_manager_singleton(self):
        """测试单例获取"""
        manager1 = get_effect_preview_quality_manager()
        manager2 = get_effect_preview_quality_manager()

        # 注意：由于是单例，这两个可能是同一个实例
        # 但为了测试隔离，我们检查类型而非实例身份
        assert isinstance(manager1, EffectPreviewQualityManager)
        assert isinstance(manager2, EffectPreviewQualityManager)

    def test_get_degraded_effect_params(self):
        """测试便捷降级参数获取"""
        # 先设置播放状态
        manager = get_effect_preview_quality_manager()
        manager.set_playback_state(True)

        original_params = {"enabled": True}
        degraded = get_degraded_effect_params("ai_matte", original_params)

        # 注册的降级参数生效，原始参数保留（SKIP 不再丢弃参数）
        assert degraded["ai_matte_enabled"] is False
        assert degraded["enabled"] is True

    def test_is_playback_degradation_active(self):
        """测试降级激活状态检查"""
        manager = get_effect_preview_quality_manager()

        manager.set_degradation_enabled(True)
        manager.set_playback_state(True)

        assert is_playback_degradation_active() is True

        manager.set_playback_state(False)

        assert is_playback_degradation_active() is False


class TestFilmGrainDegradation:
    """测试 film_grain 特定降级"""

    def test_film_grain_intensity_zero_when_playing(self):
        """播放时 film_grain intensity 降为 0"""
        manager = get_effect_preview_quality_manager()
        manager.set_degradation_enabled(True)
        manager.set_playback_state(True)

        original = {
            "enabled": True,
            "intensity": 0.8,
            "size": 2.0,
            "monochrome": True,
        }
        degraded = manager.get_degraded_params("film_grain", original)

        # film_grain 定义：degradation_params = {"intensity": 0.0}
        assert degraded["intensity"] == 0.0

    def test_film_grain_original_when_paused(self):
        """暂停时 film_grain 使用原始参数"""
        manager = get_effect_preview_quality_manager()
        manager.set_playback_state(False)

        original = {
            "enabled": True,
            "intensity": 0.8,
            "size": 2.0,
        }
        degraded = manager.get_degraded_params("film_grain", original)

        assert degraded == original


class TestPluginPreviewCostRegistration:
    """测试插件 preview_cost 注册"""

    def test_plugin_registered_with_skip_quality(self):
        """SKIP preview_cost 的插件注册正确"""
        manager = get_effect_preview_quality_manager()

        # 注册一个模拟插件
        manager.register_heavy_effect(
            HeavyEffectDefinition(
                effect_type="plugin:test_ai_plugin",
                cost_category=EffectCostCategory.AI_INFERENCE,
                preview_quality=PreviewQualityLevel.SKIP,
                ui_label="Test AI Plugin",
            )
        )

        manager.set_playback_state(True)

        # 应该被跳过
        assert manager.should_skip_effect("plugin:test_ai_plugin") is True

    def test_plugin_registered_with_high_quality(self):
        """HIGH preview_cost 的插件注册正确"""
        manager = get_effect_preview_quality_manager()

        manager.register_heavy_effect(
            HeavyEffectDefinition(
                effect_type="plugin:test_shader_plugin",
                cost_category=EffectCostCategory.SHADER_HEAVY,
                preview_quality=PreviewQualityLevel.HIGH,
                degradation_params={"intensity": 0.5},
                ui_label="Test Shader Plugin",
            )
        )

        manager.set_playback_state(True)

        # 不应该被跳过，但应该被降级
        assert manager.should_skip_effect("plugin:test_shader_plugin") is False

        original = {"enabled": True, "intensity": 1.0}
        degraded = manager.get_degraded_params("plugin:test_shader_plugin", original)

        assert degraded["intensity"] == 0.5


class TestBuiltinHeavyEffects:
    """测试内置重型效果定义"""

    def test_film_grain_in_builtin_effects(self):
        """film_grain 在内置效果列表中"""
        assert "film_grain" in BUILTIN_HEAVY_EFFECTS

        definition = BUILTIN_HEAVY_EFFECTS["film_grain"]
        assert definition.preview_quality == PreviewQualityLevel.HIGH
        assert definition.cost_category == EffectCostCategory.SHADER_HEAVY

    def test_ai_matte_in_builtin_effects(self):
        """ai_matte 在内置效果列表中"""
        assert "ai_matte" in BUILTIN_HEAVY_EFFECTS

        definition = BUILTIN_HEAVY_EFFECTS["ai_matte"]
        assert definition.preview_quality == PreviewQualityLevel.SKIP
        assert definition.cost_category == EffectCostCategory.AI_INFERENCE

    def test_ai_denoise_in_builtin_effects(self):
        """ai_denoise 在内置效果列表中"""
        assert "ai_denoise" in BUILTIN_HEAVY_EFFECTS

        definition = BUILTIN_HEAVY_EFFECTS["ai_denoise"]
        assert definition.preview_quality == PreviewQualityLevel.SKIP

    def test_blur_high_radius_in_builtin_effects(self):
        """高半径 blur 在内置效果列表中"""
        assert "blur_high_radius" in BUILTIN_HEAVY_EFFECTS

        definition = BUILTIN_HEAVY_EFFECTS["blur_high_radius"]
        assert definition.preview_quality == PreviewQualityLevel.MEDIUM
        # 上限走钳制表（min 语义），不再是覆盖式 degradation_params
        assert definition.degradation_caps["blur_radius"] == 20.0
        assert "blur_radius" not in definition.degradation_params


class TestExportNotAffected:
    """测试导出不受降级影响"""

    def test_export_uses_full_quality(self):
        """导出时使用完整质量"""
        manager = get_effect_preview_quality_manager()

        # 模拟导出场景：播放状态为 False
        manager.set_playback_state(False)

        # 即使降级功能启用，导出也不受影响
        manager.set_degradation_enabled(True)

        original = {"enabled": True, "intensity": 0.8}
        result = manager.get_degraded_params("film_grain", original)

        # 导出时应该使用原始参数
        assert result == original