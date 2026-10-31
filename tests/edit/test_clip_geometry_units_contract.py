"""DESK-06A · 变换/裁切/画布/关键帧坐标域合同测试（桌面侧冻结）。

钉住跨端坐标契约（移动端按同一单位换算；本文件不加新功能）：

1. **TransformParams 像素空间**（模型注释即契约，clip_params.py）：
   translate/pivot 像素、Y-down（translate_y 正 = 向下）、scale 1.0 =
   100% 且 ∈ [0,5]、rotation 度（顺时针正）、pivot_mode ∈ {center,
   custom}；旧归一化模型（position_x/anchor_x）按参考分辨率 1920×1080
   迁移到像素。
2. **CLIP_PARAM_RANGES 权威值域**：clip 级可动画参数的单一来源；
   关键帧参数名 ``transform.<field>`` 与 TransformParams 字段名一致。
3. **CropParams 归一化域**：单边 0..0.5、每轴合计 ≤ 0.9、feather 像素
   域；CPU 裁切把归一化值乘画布宽高换算像素（4K 换算钉住）。

待移动联调项：
- 移动端 UI 单位换算（如把像素换算成设备无关单位显示）必须可逆回
  时间线像素，不允许在持久化层换算。
- Y-down 约定与移动端渲染坐标系（CoreAnimation 亦 Y-down）一致性
  仅在联调期验证，本文件只钉桌面模型域。
"""

from __future__ import annotations

import dataclasses

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.clip_params import (
    CLIP_PARAM_RANGES,
    CropParams,
    TransformParams,
    clip_param_range,
)
from src.features.edit.core.crop_cpu import _crop_rect_from_tuple
from src.features.edit.core.crop_render import crop_rect_for_size


# ─────────────────────────────────────────────────────────────────────────────
# 1. TransformParams：像素空间 / Y-down / pivot 契约
# ─────────────────────────────────────────────────────────────────────────────

class TestTransformPixelSpace:
    def test_defaults_are_identity_in_pixel_space(self) -> None:
        t = TransformParams()
        assert t.translate_x == 0.0 and t.translate_y == 0.0
        assert t.scale_x == 1.0 and t.scale_y == 1.0
        assert t.rotation == 0.0
        assert t.pivot_mode == "center"
        assert t.pivot_x == 0.0 and t.pivot_y == 0.0

    def test_translate_and_pivot_are_unbounded_pixels(self) -> None:
        """像素平移/锚点无上限（±8K 余量在关键帧值域层，模型层不拦）。"""
        t = TransformParams(translate_x=-4000.0, translate_y=4000.0,
                            pivot_mode="custom",
                            pivot_x=-8192.0, pivot_y=8192.0)
        assert t.translate_y > 0  # Y-down：正值合法（向下）
        with pytest.raises(ValueError):
            TransformParams(translate_x=float("nan"))

    def test_scale_domain_0_to_5(self) -> None:
        TransformParams(scale_x=0.0)          # 0 合法（收拢）
        TransformParams(scale_x=5.0)          # 上界
        with pytest.raises(ValueError):
            TransformParams(scale_x=-0.1)
        with pytest.raises(ValueError):
            TransformParams(scale_y=5.1)

    def test_pivot_mode_enum(self) -> None:
        with pytest.raises(ValueError):
            TransformParams(pivot_mode="top_left")

    def test_serialization_roundtrip_pixel_model(self) -> None:
        t = TransformParams(translate_x=-100.5, translate_y=42.0,
                            scale_x=1.5, scale_y=0.8, rotation=-30.0,
                            pivot_mode="custom", pivot_x=9.0, pivot_y=-9.0)
        assert TransformParams.from_dict(t.to_dict()) == t

    def test_legacy_normalized_model_migrates_via_1920x1080(self) -> None:
        """旧 position/anchor 归一化 → 像素（参考分辨率 1920×1080）。"""
        t = TransformParams.from_dict({
            "position_x": 0.5, "position_y": 0.25,
            "anchor_x": 0.5, "anchor_y": 0.5,
        })
        assert t.translate_x == pytest.approx(960.0)   # 0.5 * 1920
        assert t.translate_y == pytest.approx(270.0)   # 0.25 * 1080
        assert t.pivot_mode == "center"
        t2 = TransformParams.from_dict({
            "position_x": 0.0, "anchor_x": 0.25, "anchor_y": 0.75,
        })
        assert t2.pivot_mode == "custom"
        assert t2.pivot_x == pytest.approx(480.0)      # 0.25 * 1920
        assert t2.pivot_y == pytest.approx(810.0)      # 0.75 * 1080

    def test_unknown_keys_survive_roundtrip(self) -> None:
        """DESK-04A：移动端新写的 transform 键桌面保真。"""
        payload = {"translate_x": 1.0, "mobile_future": 7}
        out = TransformParams.from_dict(payload).to_dict()
        assert out["mobile_future"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# 2. CLIP_PARAM_RANGES：权威值域 + 关键帧参数名
# ─────────────────────────────────────────────────────────────────────────────

class TestClipParamRanges:
    def test_frozen_range_table(self) -> None:
        assert CLIP_PARAM_RANGES == {
            "opacity": (0.0, 1.0),
            "transform.translate_x": (-8192.0, 8192.0),
            "transform.translate_y": (-8192.0, 8192.0),
            "transform.scale_x": (0.0, 5.0),
            "transform.scale_y": (0.0, 5.0),
            "transform.rotation": (-3600.0, 3600.0),
            "transform.pivot_x": (-8192.0, 8192.0),
            "transform.pivot_y": (-8192.0, 8192.0),
        }

    def test_transform_param_names_match_model_fields(self) -> None:
        """``transform.<suffix>`` 必须与 TransformParams 数值字段一一对应
        （C01：此前命令层写过 transform.position* 的错名）。
        pivot_mode 是字符串枚举不可动画，不在表内。"""
        model_fields = {
            f.name for f in dataclasses.fields(TransformParams)
            if f.name not in ("_extra_fields", "pivot_mode")
        }
        animated = {k.split(".", 1)[1] for k in CLIP_PARAM_RANGES
                    if k.startswith("transform.")}
        assert animated == model_fields

    def test_ranges_consistent_with_model_validation(self) -> None:
        """scale 值域与模型校验一致（[0,5]）。"""
        lo, hi = clip_param_range("transform.scale_x")
        assert (lo, hi) == (0.0, 5.0)
        TransformParams(scale_x=hi)
        TransformParams(scale_x=lo)

    def test_unknown_transform_param_falls_back_to_pixel_domain(self) -> None:
        assert clip_param_range("transform.future_thing") == (-8192.0, 8192.0)

    def test_non_transform_unknown_keeps_legacy_default(self) -> None:
        assert clip_param_range("effect_radius") == (0.0, 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. CropParams：归一化域 + 4K 像素换算
# ─────────────────────────────────────────────────────────────────────────────

class TestCropNormalizedDomain:
    def test_per_side_domain_0_to_05(self) -> None:
        CropParams(left=0.0, right=0.5, top=0.5, bottom=0.0)  # 边界合法
        with pytest.raises(ValueError):
            CropParams(left=0.51)
        with pytest.raises(ValueError):
            CropParams(right=-0.01)
        with pytest.raises(ValueError):
            CropParams(top=0.5001)

    def test_per_axis_sum_cap_09(self) -> None:
        CropParams(left=0.45, right=0.45)      # 合计 0.9 合法
        with pytest.raises(ValueError):
            CropParams(left=0.45, right=0.46)  # 合计 > 0.9 拒绝
        with pytest.raises(ValueError):
            CropParams(top=0.5, bottom=0.5)

    def test_feather_is_pixels_non_negative(self) -> None:
        CropParams(feather=0.0)
        CropParams(feather=64.0)
        with pytest.raises(ValueError):
            CropParams(feather=-1.0)

    def test_remaining_fractions(self) -> None:
        c = CropParams(left=0.1, right=0.2, top=0.05, bottom=0.15)
        assert c.total_horizontal_crop == pytest.approx(0.3)
        assert c.total_vertical_crop == pytest.approx(0.2)
        assert c.remaining_horizontal == pytest.approx(0.7)
        assert c.remaining_vertical == pytest.approx(0.8)

    def test_unknown_keys_survive_roundtrip(self) -> None:
        payload = {"left": 0.1, "mobile_future": True}
        out = CropParams.from_dict(payload).to_dict()
        assert out["mobile_future"] is True

    def test_pixel_conversion_at_4k(self) -> None:
        """归一化域 → 4K 画布像素换算（生产 crop_rect_for_size 同式）。"""
        crop = CropParams(left=0.1, right=0.25, top=0.5, bottom=0.0)
        rect = crop_rect_for_size(crop, 3840, 2160)
        assert rect.x0 == pytest.approx(384.0)    # 0.1 * 3840
        assert rect.x1 == pytest.approx(2880.0)   # (1-0.25) * 3840
        assert rect.y0 == pytest.approx(1080.0)   # 0.5 * 2160（Y-down：top 裁上缘）
        assert rect.y1 == pytest.approx(2160.0)   # (1-0) * 2160
        # CPU 路径（crop_cpu）与渲染路径（crop_render）几何一致
        cpu = _crop_rect_from_tuple(
            (0.1, 0.25, 0.5, 0.0, 0.0), 3840, 2160)
        assert (cpu.x0, cpu.y0, cpu.x1, cpu.y1) == (
            pytest.approx(rect.x0), pytest.approx(rect.y0),
            pytest.approx(rect.x1), pytest.approx(rect.y1))

    def test_feather_clamped_to_half_kept_side(self) -> None:
        """羽化（像素域）钳到保留区短边一半，防止倒置变全透明。"""
        crop = CropParams(top=0.4, feather=2000.0)
        rect = crop_rect_for_size(crop, 1920, 1080)
        kept_half = min((1.0 - 0.0) * 1920 - 0.0, 1080 - 0.4 * 1080) * 0.5
        assert rect.feather_px == pytest.approx(kept_half)

    def test_pixel_conversion_at_1080p(self) -> None:
        crop = CropParams(bottom=0.1)
        rect = crop_rect_for_size(crop, 1920, 1080)
        assert rect.y0 == pytest.approx(0.0)
        assert rect.y1 == pytest.approx(972.0)  # (1-0.1) * 1080：底边裁 108px
