"""四参数 Soft Clip / Edit 强度模型测试（语义权威参考：移动版）。

覆盖：默认恒等与归一化钳制、移动版 withControl 约束移植、指数软膝参考
实现的行为边界、Layer 集成（默认/序列化/重置/复制/LUT 强度）、旧工程
（v2 schema）重开/再保存效果不变的迁移契约、ColorStackData v3 字段。
"""

import copy
import math

import numpy as np
import pytest

from src.features.color.core.curves_soft_clip import (
    apply_soft_clip_channel,
    curves_soft_clip_is_identity,
    default_curves_edit,
    default_curves_soft_clip,
    normalize_curves_edit,
    normalize_curves_soft_clip,
    set_soft_clip_control,
)
from src.features.color.core.layer import Layer, build_curves_lut_from_data
from src.shared.project.color_stack import ColorStackData


# ================================================================
# 默认值与归一化
# ================================================================

class TestDefaultsAndNormalize:
    def test_default_is_linked_identity(self):
        params = default_curves_soft_clip()
        assert params["linked"] is True
        for channel in "RGB":
            assert params["channels"][channel] == {
                "low_clip": 0.0, "low_soft": 0.0,
                "high_clip": 1.0, "high_soft": 0.0,
            }

    def test_default_edit_amounts_are_neutral(self):
        amounts = default_curves_edit()
        assert amounts["linked"] is True
        assert amounts["amounts"] == {"Y": 100.0, "R": 100.0, "G": 100.0, "B": 100.0}

    def test_normalize_accepts_mobile_camel_case(self):
        # 移动版工程 JSON 使用 camelCase；导入桌面时逐键等价
        mobile = {
            "linked": False,
            "channels": {
                "red": {"lowClip": -0.5, "lowSoft": 0.2,
                        "highClip": 1.5, "highSoft": 0.3},
            },
        }
        params = normalize_curves_soft_clip(mobile)
        assert params["linked"] is False
        assert params["channels"]["R"] == {
            "low_clip": -0.5, "low_soft": 0.2,
            "high_clip": 1.5, "high_soft": 0.3,
        }
        # 未提供的通道回退默认
        assert curves_soft_clip_is_identity({**params, "linked": True}) is False
        assert params["channels"]["G"] == default_curves_soft_clip()["channels"]["G"]

    def test_normalize_clamps_into_mobile_invariants(self):
        params = normalize_curves_soft_clip({
            "channels": {"R": {
                "low_clip": 5.0,      # → clamp 到 min(1, high=1) = 1.0
                "low_soft": 9.0,      # → clamp 进 gap=0
                "high_clip": -3.0,    # → clamp 到 max(0, low=1) = 1.0
                "high_soft": -1.0,    # → 0
                "low_clip": 5.0,
            }},
        })
        values = params["channels"]["R"]
        assert values["low_clip"] == 1.0
        assert values["high_clip"] == 1.0
        assert values["low_soft"] == 0.0
        assert values["high_soft"] == 0.0
        # gap=0 时曲线退化为常量 low_clip
        assert apply_soft_clip_channel(0.5, **values) == 1.0

    def test_normalize_rejects_non_finite_values(self):
        params = normalize_curves_soft_clip({
            "channels": {"R": {"low_clip": float("nan"),
                               "high_clip": float("inf")}},
        })
        values = params["channels"]["R"]
        assert values == {"low_clip": 0.0, "low_soft": 0.0,
                          "high_clip": 1.0, "high_soft": 0.0}

    def test_normalize_invalid_structure_returns_defaults(self):
        assert normalize_curves_soft_clip(None) == default_curves_soft_clip()
        assert normalize_curves_soft_clip([1, 2]) == default_curves_soft_clip()
        assert normalize_curves_soft_clip({"channels": "bad"}) == default_curves_soft_clip()

    def test_normalize_returns_detached_copies(self):
        source = default_curves_soft_clip()
        result = normalize_curves_soft_clip(source)
        result["channels"]["R"]["low_clip"] = 0.5
        assert source["channels"]["R"]["low_clip"] == 0.0

    def test_normalize_edit_clamps_and_falls_back(self):
        amounts = normalize_curves_edit({
            "amounts": {"Y": -5, "R": 250, "G": float("nan"), "B": "bad"},
        })
        assert amounts["amounts"]["Y"] == 0.0
        assert amounts["amounts"]["R"] == 100.0
        assert amounts["amounts"]["G"] == 100.0  # 非有限回退
        assert amounts["amounts"]["B"] == 100.0


# ================================================================
# 约束（移动版 withControl 移植）
# ================================================================

class TestSetSoftClipControl:
    def test_low_clip_clamped_to_range(self):
        params = default_curves_soft_clip()
        assert set_soft_clip_control(params, "R", "low_clip", 5.0)[
            "channels"]["R"]["low_clip"] == 1.0
        assert set_soft_clip_control(params, "R", "low_clip", -7.0)[
            "channels"]["R"]["low_clip"] == -1.0

    def test_moving_low_clip_refits_knees_into_gap(self):
        params = normalize_curves_soft_clip({
            "channels": {"R": {"low_soft": 0.3, "high_soft": 0.3}},
        })
        updated = set_soft_clip_control(params, "R", "low_clip", 0.8)
        values = updated["channels"]["R"]
        assert values["low_clip"] == 0.8
        assert values["low_soft"] + values["high_soft"] <= 0.2 + 1e-12

    def test_knees_share_gap_exclusively(self):
        params = normalize_curves_soft_clip({
            "channels": {"R": {"low_soft": 0.3}},
        })
        updated = set_soft_clip_control(params, "R", "high_soft", 5.0)
        values = updated["channels"]["R"]
        assert values["high_soft"] == pytest.approx(0.7, abs=1e-12)
        assert values["low_soft"] + values["high_soft"] <= 1.0 + 1e-12

    def test_high_clip_clamped_to_range(self):
        params = normalize_curves_soft_clip({
            "channels": {"R": {"low_clip": 0.9}},
        })
        updated = set_soft_clip_control(params, "R", "high_clip", 0.5)
        assert updated["channels"]["R"]["high_clip"] == 0.9
        updated = set_soft_clip_control(params, "R", "high_clip", 9.0)
        assert updated["channels"]["R"]["high_clip"] == 2.0

    def test_returns_new_dict_without_mutating_input(self):
        params = default_curves_soft_clip()
        updated = set_soft_clip_control(params, "G", "high_soft", 0.4)
        assert params["channels"]["G"]["high_soft"] == 0.0
        assert updated["channels"]["G"]["high_soft"] == 0.4
        assert updated is not params

    def test_invalid_channel_key_value_raise(self):
        params = default_curves_soft_clip()
        with pytest.raises(ValueError):
            set_soft_clip_control(params, "Y", "low_clip", 0.0)
        with pytest.raises(ValueError):
            set_soft_clip_control(params, "R", "shoulder", 0.0)
        with pytest.raises(ValueError):
            set_soft_clip_control(params, "R", "low_clip", float("nan"))


# ================================================================
# 指数软膝参考实现（与移动版 map / soft_clip.frag 逐式对齐）
# ================================================================

class TestApplySoftClipChannel:
    def test_defaults_are_exact_identity_preserving_hdr(self):
        for value in (-0.8, -0.001, 0.0, 0.5, 1.0, 1.7, 3.5):
            assert apply_soft_clip_channel(
                value, 0.0, 0.0, 1.0, 0.0) == value

    def test_moved_high_clip_limits_super_whites(self):
        mapped = apply_soft_clip_channel(1.5, 0.0, 0.0, 0.8, 0.0)
        assert mapped == 0.8
        # 天花板以下的值不受影响
        assert apply_soft_clip_channel(0.3, 0.0, 0.0, 0.8, 0.0) == 0.3

    def test_moved_low_clip_limits_negatives(self):
        mapped = apply_soft_clip_channel(-0.5, -0.1, 0.0, 1.0, 0.0)
        assert mapped == -0.1
        assert apply_soft_clip_channel(0.2, -0.1, 0.0, 1.0, 0.0) == 0.2

    def test_high_knee_seamless_at_start(self):
        # 膝起点处斜率连续：输出 ≈ 输入
        high_clip, high_soft = 1.0, 0.3
        assert apply_soft_clip_channel(
            high_clip - high_soft, 0.0, 0.0, high_clip, high_soft
        ) == pytest.approx(high_clip - high_soft, abs=1e-9)

    def test_high_knee_asymptotes_to_clip_point(self):
        high_clip, high_soft = 1.0, 0.3
        far = apply_soft_clip_channel(4.0, 0.0, 0.0, high_clip, high_soft)
        near = apply_soft_clip_channel(
            high_clip - high_soft + 0.05, 0.0, 0.0, high_clip, high_soft)
        assert far < high_clip
        assert far > near
        # 渐近：远离膝点输出趋近 clip 但永不超过
        assert high_clip - far < high_clip - near

    def test_knee_never_amplifies_contrast(self):
        # 膝内任意小区间的斜率不超过 1（不产生额外对比度）
        high_clip, high_soft = 1.0, 0.4
        for step in np.linspace(0.0, 0.2, 40):
            y0 = apply_soft_clip_channel(
                high_clip - high_soft + step, 0.0, 0.0, high_clip, high_soft)
            y1 = apply_soft_clip_channel(
                high_clip - high_soft + step + 0.01, 0.0, 0.0, high_clip, high_soft)
            assert (y1 - y0) / 0.01 <= 1.0 + 1e-9

    def test_low_knee_mirrors_high_knee(self):
        low_clip, low_soft = 0.0, 0.25
        mapped = apply_soft_clip_channel(-1.0, low_clip, low_soft, 1.0, 0.0)
        assert mapped > low_clip  # 软膝不全剪到地板
        assert mapped < low_clip + low_soft
        # 膝起点无缝
        assert apply_soft_clip_channel(
            low_clip + low_soft, low_clip, low_soft, 1.0, 0.0
        ) == pytest.approx(low_clip + low_soft, abs=1e-9)


# ================================================================
# Layer 集成
# ================================================================

class TestLayerIntegration:
    def test_new_layer_has_identity_sidebar_state(self):
        layer = Layer()
        assert curves_soft_clip_is_identity(layer.curves_soft_clip)
        assert layer.curves_edit == default_curves_edit()

    def test_roundtrip_preserves_custom_values(self):
        layer = Layer()
        layer.curves_soft_clip = normalize_curves_soft_clip({
            "linked": False,
            "channels": {"R": {"low_clip": -0.2, "low_soft": 0.1,
                               "high_clip": 1.3, "high_soft": 0.2}},
        })
        layer.curves_edit = normalize_curves_edit({
            "linked": False, "amounts": {"Y": 60.0, "R": 80.0},
        })
        restored = Layer.from_dict(layer.to_dict(None))
        assert restored.curves_soft_clip == layer.curves_soft_clip
        assert restored.curves_edit == layer.curves_edit

    def test_legacy_v2_layer_dict_loads_identity_and_resaves_stable(self):
        # v2 工程的 Layer dict 没有侧栏键：载入为恒等，再保存不改动任何
        # 旧字段（任务 3 迁移契约：重开/再保存视觉效果不变）
        legacy = Layer("legacy").to_dict(None)
        legacy.pop("curves_soft_clip")
        legacy.pop("curves_edit")
        legacy["curves_data"] = {"M": [(0, 255), (128, 200), (255, 0)]}
        legacy["soft_clip_params"] = {
            "enabled": True, "high": 0.9, "white_point": 1.2,
            "shoulder_strength": 1.5, "low": 0.02,
            "black_point": -0.05, "toe_strength": 1.2,
            "exposure": 0.1, "contrast": 0.05,
        }
        restored = Layer.from_dict(copy.deepcopy(legacy))
        assert curves_soft_clip_is_identity(restored.curves_soft_clip)
        assert restored.curves_edit == default_curves_edit()

        resaved = restored.to_dict(None)
        for key, value in legacy.items():
            assert resaved[key] == value, f"旧字段 {key} 被迁移改动"

    def test_reset_params_restores_defaults(self):
        layer = Layer()
        layer.curves_soft_clip = set_soft_clip_control(
            layer.curves_soft_clip, "B", "high_soft", 0.5)
        layer.curves_edit = normalize_curves_edit({"amounts": {"G": 10.0}})
        layer.reset_params()
        assert curves_soft_clip_is_identity(layer.curves_soft_clip)
        assert layer.curves_edit == default_curves_edit()

    def test_copy_params_from_deepcopies_sidebar_state(self):
        source = Layer()
        source.curves_soft_clip = set_soft_clip_control(
            source.curves_soft_clip, "R", "low_clip", -0.4)
        target = Layer()
        target.copy_params_from(source)
        assert target.curves_soft_clip["channels"]["R"]["low_clip"] == -0.4
        target.curves_soft_clip["channels"]["R"]["low_clip"] = 0.0
        assert source.curves_soft_clip["channels"]["R"]["low_clip"] == -0.4

    def test_has_grading_effects_detects_sidebar_adjustments(self):
        layer = Layer()
        assert layer.has_grading_effects() is False
        layer.curves_soft_clip = set_soft_clip_control(
            layer.curves_soft_clip, "R", "high_soft", 0.3)
        assert layer.has_grading_effects() is True
        layer.reset_params()
        layer.curves_edit = normalize_curves_edit({"amounts": {"Y": 50.0}})
        assert layer.has_grading_effects() is True


# ================================================================
# 曲线 LUT 合成与强度
# ================================================================

class TestCurvesLutComposition:
    @staticmethod
    def _identity_ramp() -> np.ndarray:
        return (np.arange(256, dtype=np.float32) / 255.0)[:, None] * np.ones((256, 1, 3), dtype=np.float32)

    def test_empty_curves_yield_identity_ramp(self):
        lut = build_curves_lut_from_data({})
        assert np.allclose(lut[:, 0, 0], np.arange(256) / 255.0, atol=1e-6)
        assert np.allclose(lut[:, 0, 1], lut[:, 0, 0])
        assert np.allclose(lut[:, 0, 2], lut[:, 0, 0])

    def test_master_only_lut(self):
        lut = build_curves_lut_from_data({"M": [(0, 255), (255, 128)]})
        # UI 倒置 Y：输出 = (255-y)/255 → y=255(黑)→0.0，y=128 → 127/255
        assert lut[0, 0, 0] == pytest.approx(0.0, abs=1e-6)
        assert lut[255, 0, 0] == pytest.approx(127.0 / 255.0, abs=1e-6)
        assert np.allclose(lut[:, 0, 1], lut[:, 0, 0])

    def test_channel_curve_composes_over_master_like_widget(self):
        # master 抬高中间调，R 曲线再在其上叠加：合成 = C(M(x))
        master = {"M": [(0, 255), (128, 210), (255, 0)]}
        with_channel = {"M": master["M"], "R": [(0, 255), (255, 30)]}
        lut = build_curves_lut_from_data(with_channel)
        # R 列在输入 255 处：master(255)=1.0，R 曲线在索引 255 处 = 30 反转 → (255-30)/255
        expected = (255.0 - 30.0) / 255.0
        assert lut[255, 0, 0] == pytest.approx(expected, abs=1e-5)
        # G/B 列只含 master
        master_only = build_curves_lut_from_data(master)
        assert np.allclose(lut[:, 0, 1], master_only[:, 0, 1], atol=1e-6)

    def test_edit_amount_y_zero_bypasses_master(self):
        data = {"M": [(0, 255), (255, 128)], "R": [(0, 255), (255, 0)]}
        full = build_curves_lut_from_data(data)
        muted = build_curves_lut_from_data(data, {"Y": 0.0})
        assert not np.allclose(full[:, 0, 0], muted[:, 0, 0])
        # master 被旁路：输出 = 通道恒等
        assert np.allclose(muted[:, 0, 0], np.arange(256) / 255.0, atol=1e-6)

    def test_edit_amount_channel_zero_bypasses_channel_curve(self):
        data = {"M": [(0, 255), (255, 0)], "R": [(0, 255), (128, 100), (255, 0)]}
        full = build_curves_lut_from_data(data)
        muted = build_curves_lut_from_data(data, {"R": 0.0})
        assert not np.allclose(full[:, 0, 0], muted[:, 0, 0])
        assert np.allclose(muted[:, 0, 0], np.arange(256) / 255.0, atol=1e-6)

    def test_edit_amount_scales_channel_delta_linearly(self):
        data = {"M": [(0, 255), (255, 0)], "R": [(0, 255), (128, 100), (255, 0)]}
        full = build_curves_lut_from_data(data)[:, 0, 0]
        identity = np.arange(256) / 255.0
        half = build_curves_lut_from_data(data, {"R": 50.0})[:, 0, 0]
        expected = identity + 0.5 * (full - identity)
        assert np.allclose(half, expected, atol=1e-5)

    def test_amounts_default_matches_no_amounts(self):
        data = {"M": [(0, 255), (128, 210), (255, 0)],
                "R": [(0, 255), (255, 30)], "G": [(0, 255), (255, 40)]}
        plain = build_curves_lut_from_data(data)
        with_full = build_curves_lut_from_data(
            data, {"Y": 100.0, "R": 100.0, "G": 100.0, "B": 100.0})
        assert np.allclose(plain, with_full, atol=1e-6)


# ================================================================
# ColorStackData v3
# ================================================================

class TestColorStackDataV3:
    def test_new_stack_defaults_empty(self):
        stack = ColorStackData.create()
        assert stack.curves_soft_clip == {}
        assert stack.curves_edit == {}

    def test_v3_fields_roundtrip(self):
        stack = ColorStackData.create()
        state = set_soft_clip_control(
            default_curves_soft_clip(), "G", "low_clip", -0.3)
        stack = stack.with_curves_soft_clip(state).with_curves_edit(
            {"linked": True, "amounts": {"Y": 70.0}})
        payload = stack.to_dict()
        assert payload["schema_version"] >= 3
        restored = ColorStackData.from_dict(payload)
        assert restored.curves_soft_clip == state
        assert restored.curves_edit == {"linked": True, "amounts": {"Y": 70.0}}

    def test_v2_payload_without_v3_keys_opens(self):
        payload = ColorStackData.create().to_dict()
        payload.pop("curves_soft_clip", None)
        payload.pop("curves_edit", None)
        restored = ColorStackData.from_dict(payload)
        assert restored.curves_soft_clip == {}
        assert restored.curves_edit == {}

    def test_non_default_fields_are_persisted_conditionally(self):
        # 未触碰侧栏的栈输出形状与 v2 稳定（不含 v3 键）
        payload = ColorStackData.create().to_dict()
        assert "curves_soft_clip" not in payload
        assert "curves_edit" not in payload

    def test_unknown_keys_preserved_through_roundtrip(self):
        payload = ColorStackData.create().to_dict()
        payload["future_extension"] = {"anything": [1, 2, 3]}
        restored = ColorStackData.from_dict(payload)
        assert restored.to_dict()["future_extension"] == {"anything": [1, 2, 3]}
