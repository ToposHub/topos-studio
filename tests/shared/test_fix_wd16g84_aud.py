"""
wd16-g84 修复验证测试：src/shared/effects/preview_quality.py

覆盖条目：
- AUD-1101 [Major/risk] 无锁共享可变状态 + 非线程安全懒汉式单例
- AUD-1102 [Major/bug]  导出路径无保护（preview/export 语义一致性）
- AUD-1103 [Major/bug]  播放中重新启用降级后 degraded_effects 不重建
- AUD-1104 [Major/bug]  降级参数覆盖式合并而非钳制（反向增大计算量）
- AUD-1105 [Major/bug]  SKIP 路径硬编码 {"enabled": False} 丢弃原始参数

（AUD-1100 为白名单外接线问题，见 fixes/reports/wd16-g84.md，不在此测试。）

运行：
    python -m pytest tests/shared/test_fix_wd16g84_aud.py -v
"""

import threading
from typing import List

import pytest

from src.shared.effects import preview_quality as pq
from src.shared.effects.preview_quality import (
    BLUR_HIGH_RADIUS_PLAYBACK_CAP,
    BUILTIN_HEAVY_EFFECTS,
    EffectCostCategory,
    EffectPreviewQualityManager,
    HeavyEffectDefinition,
    PreviewQualityLevel,
)


@pytest.fixture()
def manager() -> EffectPreviewQualityManager:
    """独立管理器实例，避免污染全局单例。"""
    m = EffectPreviewQualityManager()
    m.set_degradation_enabled(True)
    return m


def _custom_effect(effect_type: str, level: PreviewQualityLevel) -> HeavyEffectDefinition:
    return HeavyEffectDefinition(
        effect_type=effect_type,
        cost_category=EffectCostCategory.SHADER_HEAVY,
        preview_quality=level,
        ui_label=effect_type,
    )


# =============================================================================
# AUD-1105：SKIP 路径合并注册的 degradation_params，不再丢弃原始参数
# =============================================================================

class TestAud1105SkipMerge:

    def test_skip_ai_matte_preserves_original_and_applies_registered_params(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["ai_matte"]
        original = {"enabled": True, "model": "modnet.onnx", "threshold": 0.5}

        degraded = definition.get_degraded_params(original)

        # 注册的 ai_matte_enabled 真正生效（修复前是死配置）
        assert degraded["ai_matte_enabled"] is False
        # 原始参数全部保留（修复前仅返回 {"enabled": False}）
        assert degraded == {**original, "ai_matte_enabled": False}

    def test_skip_ai_matte_update_uses_update_enabled(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["ai_matte_update"]
        degraded = definition.get_degraded_params({"enabled": True, "interval": 3})

        assert degraded["update_enabled"] is False
        assert degraded["interval"] == 3
        assert degraded["enabled"] is True

    def test_skip_denoise_disables_via_registered_enabled(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["denoise"]
        degraded = definition.get_degraded_params({"enabled": True, "strength": 5})

        assert degraded["enabled"] is False
        assert degraded["strength"] == 5

    def test_skip_plugin_convention_keeps_params(self) -> None:
        """plugin_manager 注册的 plugin:*（degradation_params={"enabled": False}）走同一合并规则。"""
        definition = HeavyEffectDefinition(
            effect_type="plugin:demo",
            cost_category=EffectCostCategory.AI_INFERENCE,
            preview_quality=PreviewQualityLevel.SKIP,
            degradation_params={"enabled": False},
        )
        degraded = definition.get_degraded_params({"enabled": True, "amount": 0.7})

        assert degraded == {"enabled": False, "amount": 0.7}

    def test_manager_level_skip_merges_original_params(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)

        degraded = manager.get_degraded_params("ai_matte", {"enabled": True, "model": "m.onnx"})

        assert degraded["ai_matte_enabled"] is False
        assert degraded["model"] == "m.onnx"
        assert degraded["enabled"] is True


# =============================================================================
# AUD-1104：降级参数钳制（min），不反向增大计算量/改变画面
# =============================================================================

class TestAud1104Clamp:

    def test_blur_radius_below_cap_is_not_raised(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["blur_high_radius"]
        degraded = definition.get_degraded_params({"blur_radius": 10.0, "blur_iterations": 3})

        # 修复前：覆盖式合并会把 10.0 抬升到 20.0（成本反而升高）
        assert degraded["blur_radius"] == 10.0
        assert degraded["blur_iterations"] == 1

    def test_blur_radius_above_cap_is_capped(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["blur_high_radius"]
        degraded = definition.get_degraded_params({"blur_radius": 40.0, "blur_iterations": 2})

        assert degraded["blur_radius"] == BLUR_HIGH_RADIUS_PLAYBACK_CAP
        assert degraded["blur_iterations"] == 1

    def test_mask_scale_not_raised(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["full_res_mask"]

        already_low = definition.get_degraded_params({"mask_scale": 0.25})
        full = definition.get_degraded_params({"mask_scale": 1.0})

        # 修复前：0.25 会被抬升到 0.5（分辨率反而升高）
        assert already_low["mask_scale"] == 0.25
        assert full["mask_scale"] == 0.5

    def test_caps_do_not_inject_missing_keys(self) -> None:
        definition = BUILTIN_HEAVY_EFFECTS["blur_high_radius"]
        degraded = definition.get_degraded_params({"blur_iterations": 3})

        assert "blur_radius" not in degraded
        assert degraded["blur_iterations"] == 1

    def test_caps_do_not_touch_bool_values(self) -> None:
        definition = HeavyEffectDefinition(
            effect_type="cap_bool",
            cost_category=EffectCostCategory.SHADER_HEAVY,
            preview_quality=PreviewQualityLevel.MEDIUM,
            degradation_caps={"enabled": 1},
        )
        degraded = definition.get_degraded_params({"enabled": True})

        assert degraded["enabled"] is True

    def test_override_params_still_applied_for_high_level(self) -> None:
        """film_grain（HIGH，intensity=0.0 覆盖）行为不变。"""
        definition = BUILTIN_HEAVY_EFFECTS["film_grain"]
        degraded = definition.get_degraded_params({"enabled": True, "intensity": 0.8})

        assert degraded["intensity"] == 0.0
        assert degraded["enabled"] is True

    def test_manager_api_blur_cap_consistent_with_consumer_path(self, manager: EffectPreviewQualityManager) -> None:
        """video_playback.py 的 blur 限流语义（>20 才触发、限制到 20）与管理器 API 一致。"""
        manager.set_playback_state(True)

        capped = manager.get_degraded_params(
            "blur_high_radius", {"blur_radius": 30.0, "blur_iterations": 2}
        )
        untouched_ok = manager.get_degraded_params(
            "blur_high_radius", {"blur_radius": 10.0}
        )

        assert capped["blur_radius"] == BLUR_HIGH_RADIUS_PLAYBACK_CAP
        assert untouched_ok["blur_radius"] == 10.0


# =============================================================================
# AUD-1103：播放中重新启用降级后重建 degraded_effects，决策与状态一致
# =============================================================================

class TestAud1103ReenableRebuild:

    def test_reenable_during_playback_rebuilds_degraded_list(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)
        assert manager.is_effect_degraded("blur_high_radius") is True

        manager.set_degradation_enabled(False)
        assert manager.is_effect_degraded("blur_high_radius") is False
        assert manager.get_degradation_summary()["degraded_count"] == 0

        # 修复前：重新启用后 degraded_effects 保持为空（只处理了关闭分支）
        manager.set_degradation_enabled(True)
        assert manager.is_effect_degraded("blur_high_radius") is True
        assert manager.get_degradation_summary()["degraded_count"] > 0

    def test_reenable_restores_param_degradation_for_consumer_path(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)
        params = {"blur_radius": 30.0}

        manager.set_degradation_enabled(False)
        assert manager.get_degraded_params("blur_high_radius", params) == params

        manager.set_degradation_enabled(True)
        assert manager.get_degraded_params("blur_high_radius", params)["blur_radius"] == 20.0

    def test_decision_paths_and_state_tracking_stay_consistent(self, manager: EffectPreviewQualityManager) -> None:
        """同帧内 get_degraded_params/should_skip_effect/is_effect_degraded 不再分裂。"""
        manager.set_playback_state(True)

        for enabled in (False, True):
            manager.set_degradation_enabled(enabled)
            degraded_flag = manager.is_effect_degraded("film_grain")
            degraded_params = manager.get_degraded_params(
                "film_grain", {"intensity": 0.8}
            )["intensity"] == 0.0
            assert degraded_flag == degraded_params == enabled
            assert manager.should_skip_effect("ai_matte") is enabled

    def test_register_during_playback_degrades_immediately(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)

        manager.register_heavy_effect(_custom_effect("late_effect", PreviewQualityLevel.SKIP))

        assert manager.should_skip_effect("late_effect") is True
        assert manager.is_effect_degraded("late_effect") is True

        manager.unregister_heavy_effect("late_effect")
        assert manager.should_skip_effect("late_effect") is False
        assert manager.is_effect_degraded("late_effect") is False


# =============================================================================
# AUD-1102：导出保护（导出期间强制完整质量并冻结状态切换）
# =============================================================================

class TestAud1102ExportGuard:

    def test_export_context_forces_full_quality(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)
        original = {"enabled": True, "intensity": 0.8}
        assert manager.get_degraded_params("film_grain", original) != original

        with manager.export_full_quality():
            assert manager.is_export_active() is True
            assert manager.get_degraded_params("film_grain", original) == original
            assert manager.should_skip_effect("ai_matte") is False
            assert manager.is_effect_degraded("film_grain") is False
            assert manager.get_degradation_summary()["degraded_count"] == 0

        # 退出导出后按最新状态恢复降级
        assert manager.is_export_active() is False
        assert manager.get_degraded_params("film_grain", original) != original

    def test_export_freezes_state_switches(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)
        manager.begin_export()

        # 导出期间切换开关：只记录标志，不重建/不清空降级列表
        manager.set_degradation_enabled(False)
        manager.set_degradation_enabled(True)
        manager.set_playback_state(False)
        manager.set_playback_state(True)
        assert manager.is_export_active() is True

        manager.end_export()
        # 退出导出后按最新状态（播放中 + 启用降级）重建
        assert manager.is_effect_degraded("film_grain") is True

    def test_export_scope_is_exception_safe(self, manager: EffectPreviewQualityManager) -> None:
        with pytest.raises(RuntimeError):
            with manager.export_full_quality():
                raise RuntimeError("boom")

        assert manager.is_export_active() is False

    def test_end_export_without_begin_is_noop(self, manager: EffectPreviewQualityManager) -> None:
        manager.set_playback_state(True)

        manager.end_export()

        assert manager.is_export_active() is False
        assert manager.is_effect_degraded("film_grain") is True

    def test_module_level_activation_flag_respects_export(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = EffectPreviewQualityManager()
        monkeypatch.setattr(pq, "_manager_instance", m)

        m.set_degradation_enabled(True)
        m.set_playback_state(True)
        assert pq.is_playback_degradation_active() is True

        with m.export_full_quality():
            assert pq.is_playback_degradation_active() is False

        assert pq.is_playback_degradation_active() is True


# =============================================================================
# AUD-1101：线程安全（单例双重检查加锁 + 管理器内部锁 + 状态快照）
# =============================================================================

class TestAud1101ThreadSafety:

    def test_singleton_concurrent_initialization_returns_single_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pq, "_manager_instance", None)

        workers = 8
        barrier = threading.Barrier(workers)
        results: List[EffectPreviewQualityManager] = []
        errors: List[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                results.append(pq.get_effect_preview_quality_manager())
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"worker errors: {errors}"
        assert len(results) == workers
        assert len({id(m) for m in results}) == 1

    def test_concurrent_register_and_query_no_iteration_error(
        self, manager: EffectPreviewQualityManager
    ) -> None:
        """并发 注册/注销（写注册表） vs 查询遍历（读降级列表/注册表）不再抛
        RuntimeError: changed size during iteration。"""
        manager.set_playback_state(True)
        errors: List[BaseException] = []
        stop = threading.Event()

        def registrar() -> None:
            try:
                for i in range(300):
                    manager.register_heavy_effect(
                        _custom_effect(f"late_{i}", PreviewQualityLevel.MEDIUM)
                    )
                    manager.unregister_heavy_effect(f"late_{i}")
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)
            finally:
                stop.set()

        def toggler() -> None:
            try:
                for _ in range(150):
                    manager.set_degradation_enabled(False)
                    manager.set_degradation_enabled(True)
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [
            threading.Thread(target=registrar),
            threading.Thread(target=toggler),
        ]
        for t in threads:
            t.start()

        try:
            for _ in range(300):
                manager.get_degradation_summary()
                manager.get_all_heavy_effects()
                manager.should_skip_effect("ai_matte")
                manager.get_degraded_params("film_grain", {"intensity": 0.8})
                if stop.is_set():
                    break
        except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)
        finally:
            for t in threads:
                t.join(timeout=10.0)

        assert not errors, f"concurrent access errors: {errors}"

    def test_get_degradation_state_returns_snapshot_copy(
        self, manager: EffectPreviewQualityManager
    ) -> None:
        manager.set_playback_state(True)

        snapshot = manager.get_degradation_state()
        snapshot.clear_degraded()
        snapshot.add_degraded("bogus_effect")

        # 管理器内部状态不受返回值修改影响
        assert manager.is_effect_degraded("film_grain") is True
        assert manager.is_effect_degraded("bogus_effect") is False

        fresh = manager.get_degradation_state()
        assert fresh.degraded_effects == manager.get_degradation_state().degraded_effects
        assert fresh is not manager.get_degradation_state()
